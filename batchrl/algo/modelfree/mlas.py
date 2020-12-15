# PLAS: Latent Action Space for Offline Reinforcement Learning
# https://sites.google.com/view/latent-policy
# https://github.com/Wenxuan-Zhou/PLAS
import abc
import copy
from collections import OrderedDict

import torch
import numpy as np
from torch import nn
from torch import optim
from loguru import logger
import torch.nn.functional as F

from batchrl.utils.data import to_torch,get_scaler
from batchrl.algo.base import BaseAlgo
from batchrl.utils.net.common import Net
from batchrl.utils.net.mlas import VAE,ActorPerturbation
from batchrl.utils.net.continuous import Critic, Actor


def algo_init(args):
    logger.info('Run algo_init function')
    
    if args["obs_shape"] and args["action_shape"]:
        obs_shape, action_shape = args["obs_shape"], args["action_shape"]
    elif "task" in args.keys():
        from batchrl.utils.env import get_env_shape, get_env_state_range
        obs_shape, action_shape = get_env_shape(args['task'])
        max_action,min_action = 1, -1
        args["obs_shape"], args["action_shape"] = obs_shape, action_shape
    else:
        raise NotImplementedError
        
    print("max_action:",max_action)
        
    latent_dim = obs_shape *2
    vae = VAE(state_dim = obs_shape + action_shape, 
              action_dim = obs_shape, 
              latent_dim = latent_dim, 
              max_action = max_action,
              hidden_size=args["vae_hidden_size"]).to(args['device'])
    
    vae_opt = optim.Adam(vae.parameters(), lr=args["vae_lr"])
    


    if args["latent"]:
        actor = ActorPerturbation(obs_shape + action_shape, 
                                  action_shape, 
                                  latent_dim, 
                                  max_action,
                                  max_latent_action=2*max_action, 
                                  phi=0.05).to(args['device'])
        
    else:
        net_a = Net(layer_num = args["layer_num"], 
                    state_shape = obs_shape + action_shape, 
                    hidden_layer_size = args["hidden_layer_size"])
        actor = Actor(preprocess_net = net_a,
                     action_shape = latent_dim,
                     max_action = max_action,
                     hidden_layer_size = args["hidden_layer_size"]).to(args['device'])

    
    actor_opt = optim.Adam(actor.parameters(), lr=args["actor_lr"])
    
    net_c1 = Net(layer_num = args['layer_num'],
                  state_shape = obs_shape + action_shape,  
                  action_shape = obs_shape,
                  concat = True, 
                  hidden_layer_size = args['hidden_layer_size'])
    critic1 = Critic(preprocess_net = net_c1, 
                     hidden_layer_size = args['hidden_layer_size'],
                    ).to(args['device'])
    critic1_opt = optim.Adam(critic1.parameters(), lr=args['critic_lr'])
    
    net_c2 = Net(layer_num = args['layer_num'],
                  state_shape = obs_shape + action_shape,  
                  action_shape = obs_shape,
                  concat = True, 
                  hidden_layer_size = args['hidden_layer_size'])
    critic2 = Critic(preprocess_net = net_c2, 
                     hidden_layer_size = args['hidden_layer_size'],
                    ).to(args['device'])
    critic2_opt = optim.Adam(critic2.parameters(), lr=args['critic_lr'])
    
    return {
        "vae" : {"net" : vae, "opt" : vae_opt},
        "actor" : {"net" : actor, "opt" : actor_opt},
        "critic1" : {"net" : critic1, "opt" : critic1_opt},
        "critic2" : {"net" : critic2, "opt" : critic2_opt},
    }

class eval_policy():
    def __init__(self, vae, actor):
        self.vae = vae
        self.actor = actor

    def get_action(self, state):
        with torch.no_grad():
            state = torch.FloatTensor(state.reshape(1, -1)).to(self.vae.device)
            action = self.vae.decode(state, z=self.actor(state)[0])
        return action.cpu().data.numpy().flatten()


class AlgoTrainer(BaseAlgo):
    def __init__(self, algo_init, args):
        super(AlgoTrainer, self).__init__(args)
        
        self.vae = algo_init["vae"]["net"]
        self.vae_opt = algo_init["vae"]["opt"]
        
        self.actor = algo_init["actor"]["net"]
        self.actor_opt = algo_init["actor"]["opt"]

        self.critic1 = algo_init["critic1"]["net"]
        self.critic1_opt = algo_init["critic1"]["opt"]
        
        self.critic2 = algo_init["critic2"]["net"]
        self.critic2_opt = algo_init["critic2"]["opt"]
        
        self.actor_target = copy.deepcopy(self.actor)
        self.critic1_target = copy.deepcopy(self.critic1)
        self.critic2_target = copy.deepcopy(self.critic2)
        
        self.args = args

        
    def _train_vae_step(self, batch):
        batch = to_torch(batch, torch.float, device=self.args["device"])
        obs = batch.obs
        act = batch.act
        obs_next = batch.obs_next
        
        recon, mean, std = self.vae(torch.cat([obs, act], axis=1), obs_next)
        recon_loss = F.mse_loss(recon, obs_next)
        KL_loss = -self.args["vae_kl_weight"] * (1 + torch.log(std.pow(2)) - mean.pow(2) - std.pow(2)).mean()
        vae_loss = recon_loss + 0.5 * KL_loss

        self.vae_opt.zero_grad()
        vae_loss.backward()
        self.vae_opt.step()
        

        
        return vae_loss.cpu().data.numpy(), recon_loss.cpu().data.numpy(), KL_loss.cpu().data.numpy()
        
    def _train_vae(self, replay_buffer):
        logs = {'vae_loss': [], 'recon_loss': [], 'kl_loss': []}
        for i in range(self.args["vae_iterations"]):
            batch = replay_buffer.sample(self.args["vae_batch_size"])
            vae_loss, recon_loss, KL_loss = self._train_vae_step(batch)
            logs['vae_loss'].append(vae_loss)
            logs['recon_loss'].append(recon_loss)
            logs['kl_loss'].append(KL_loss)
            if (i + 1) % 1000 == 0:
                logger.info('VAE Epoch : {}, KL_loss : {:.4}', (i + 1) // 1000, KL_loss)
                logger.info('VAE Epoch : {}, recon_loss : {:.4}', (i + 1) // 1000, recon_loss)
                logger.info('VAE Epoch : {}, Loss : {:.4}', (i + 1) // 1000, vae_loss)

                #self.log_res((i + 1) // 1000, {"VaeLoss" : vae_loss.item(), "Reconloss" : recon_loss.item(), "KLLoss":KL_loss.item()})

        logger.info('Save VAE Model -> {}', "/tmp/vae_"+str(i)+".pkl")
        #torch.save(self.vae, "/tmp/vae_"+str(i)+".pkl") 

        
    def _train_policy(self, replay_buffer, eval_fn):
        for it in range(self.args["actor_iterations"]):
            batch = replay_buffer.sample(self.args["actor_batch_size"])
            batch = to_torch(batch, torch.float, device=self.args["device"])
            rew = batch.rew
            done = batch.done
            obs = batch.obs
            act = batch.act
            obs_next = batch.obs_next
            
            obs = torch.cat([obs,act],axis=1)
            act = obs_next
            
            
            # Actor Training
            action_actor,_ = self.actor(obs)

            action_vae = self.vae.decode(obs, z = action_actor)
            
            actor_loss = F.mse_loss(action_vae, act).mean()

            
            self.actor.zero_grad()
            actor_loss.backward()
            self.actor_opt.step()

            
            if (it + 1) % 1000 == 0:
                if eval_fn is None:
                    self.eval_policy()
                else:
                    self.vae._actor = copy.deepcopy(self.actor)
                    res = eval_fn(self.get_policy(),obs_scaler = self.obs_scaler)
                    self.log_res((it + 1) // 1000, res)
                    
                    
    def _train_policy_latent(self, replay_buffer, eval_fn):
        for it in range(self.args["actor_iterations"]):
            batch = replay_buffer.sample(self.args["actor_batch_size"])
            batch = to_torch(batch, torch.float, device=self.args["device"])
            rew = batch.rew
            done = batch.done
            obs = batch.obs
            act = batch.act
            obs_next = batch.obs_next

            # Critic Training
            with torch.no_grad():
                _, _, next_action = self.actor_target(obs_next, self.vae.decode)

                target_q1 = self.critic1_target(obs_next, next_action)
                target_q2 = self.critic2_target(obs_next, next_action)
 
                target_q = self.args["lmbda"] * torch.min(target_q1, target_q2) + (1 - self.args["lmbda"]) * torch.max(target_q1, target_q2)
                target_q = rew + (1 - done) * self.args["discount"] * target_q

            current_q1 = self.critic1(obs, act)
            current_q2 = self.critic2(obs, act)

            critic_loss = F.mse_loss(current_q1, target_q) + F.mse_loss(current_q2, target_q)

            self.critic1_opt.zero_grad()
            self.critic2_opt.zero_grad()
            critic_loss.backward()
            self.critic1_opt.step()
            self.critic2_opt.step()
            
            # Actor Training
            latent_actions, mid_actions, actions = self.actor(obs, self.vae.decode)
            actor_loss = -self.critic1(obs, actions).mean()
            
            self.actor.zero_grad()
            actor_loss.backward()
            self.actor_opt.step()

            # update target network
            self._sync_weight(self.actor_target, self.actor)
            self._sync_weight(self.critic1_target, self.critic1)
            self._sync_weight(self.critic2_target, self.critic2)
            
            if (it + 1) % 1000 == 0:
                print("mid_actions :",torch.abs(actions - mid_actions).mean())
                if eval_fn is None:
                    self.eval_policy()
                else:
                    self.vae._actor = copy.deepcopy(self.actor)
                    print(self.obs_scaler)
                    res = eval_fn(self.get_policy())
                    self.log_res((it + 1) // 1000, res)
    
                    
    def get_model(self):
        pass
    
    def save_model(self):
        torch.save(self.get_policy(), "/tmp/env_"+str(i)+".pkl") 
    
    def get_policy(self):
        if self.args["latent"]:
            self.actor.vae = copy.deepcopy(self.vae)
            return self.actor
        else:
            self.vae._actor = copy.deepcopy(self.actor)
            return self.vae
            
    def train(self, replay_buffer, callback_fn=None):
        #"""

        #self.vae = torch.load("/tmp/vae_499999.pkl").to(self.args["device"])
        self.obs_scaler = get_scaler(np.concatenate([replay_buffer.obs,replay_buffer.obs_next], axis=0))
        replay_buffer.obs = self.obs_scaler.transform(replay_buffer.obs)
        replay_buffer.obs_next = self.obs_scaler.transform(replay_buffer.obs_next)
        
        self._train_vae(replay_buffer) 
        self.vae.eval()
        if self.args["latent"]:
            self._train_policy_latent(replay_buffer, callback_fn)
        else:
            self._train_policy(replay_buffer, callback_fn)