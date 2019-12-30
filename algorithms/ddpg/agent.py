# -*- coding: utf-8 -*-
"""DDPG agent for episodic tasks in OpenAI Gym.

- Author: Curt Park
- Contact: curt.park@medipixel.io
- Paper: https://arxiv.org/pdf/1509.02971.pdf
"""

import argparse
import os
import shutil
import time
from typing import Tuple

import gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import wandb

from algorithms.common.abstract.agent import Agent
from algorithms.common.buffer.replay_buffer import ReplayBuffer
import algorithms.common.helper_functions as common_utils
from algorithms.common.networks.mlp import MLP
from algorithms.common.noise import OUNoise
from algorithms.registry import AGENTS
from algorithms.utils.config import ConfigDict

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


@AGENTS.register_module
class DDPGAgent(Agent):
    """ActorCritic interacting with environment.

    Attributes:
        memory (ReplayBuffer): replay memory
        noise (OUNoise): random noise for exploration
        actor (nn.Module): actor model to select actions
        actor_target (nn.Module): target actor model to select actions
        critic (nn.Module): critic model to predict state values
        critic_target (nn.Module): target critic model to predict state values
        actor_optim (Optimizer): optimizer for training actor
        critic_optim (Optimizer): optimizer for training critic
        curr_state (np.ndarray): temporary storage of the current state
        total_step (int): total step numbers
        episode_step (int): step number of the current episode
        i_episode (int): current episode number

    """

    def __init__(
        self,
        env: gym.Env,
        args: argparse.Namespace,
        log_cfg: ConfigDict,
        params: ConfigDict,
        network_cfg: ConfigDict,
        optim_cfg: ConfigDict,
        noise_cfg: ConfigDict,
    ):
        """Initialization.

        Args:
            env (gym.Env): openAI Gym environment
            args (argparse.Namespace): arguments including hyperparameters and training settings

        """
        Agent.__init__(self, env, args, log_cfg)

        self.curr_state = np.zeros((1,))
        self.total_step = 0
        self.episode_step = 0
        self.i_episode = 0

        self.params = params
        self.gamma = params.gamma
        self.tau = params.tau
        self.buffer_size = params.buffer_size
        self.batch_size = params.batch_size
        self.initial_random_action = params.initial_random_action

        self.multiple_learn = params.multiple_learn
        self.gradient_clip_ac = params.gradient_clip_ac
        self.gradient_clip_cr = params.gradient_clip_cr
        self.network_cfg = network_cfg
        self.optim_cfg = optim_cfg

        self.state_dim = self.env.observation_space.shape[0]
        self.action_dim = self.env.action_space.shape[0]

        # set noise
        self.noise = OUNoise(
            self.action_dim,
            theta=noise_cfg.ou_noise_theta,
            sigma=noise_cfg.ou_noise_sigma,
        )

        self._initialize()

    # pylint: disable=attribute-defined-outside-init
    def _init_network(self):
        """Initialize networks and optimizers."""
        # create actor
        self.actor = MLP(
            input_size=self.state_dim,
            output_size=self.action_dim,
            hidden_sizes=self.network_cfg.hidden_sizes_actor,
            output_activation=torch.tanh,
        ).to(device)

        self.actor_target = MLP(
            input_size=self.state_dim,
            output_size=self.action_dim,
            hidden_sizes=self.network_cfg.hidden_sizes_actor,
            output_activation=torch.tanh,
        ).to(device)
        self.actor_target.load_state_dict(self.actor.state_dict())

        # create critic
        self.critic = MLP(
            input_size=self.state_dim + self.action_dim,
            output_size=1,
            hidden_sizes=self.network_cfg.hidden_sizes_critic,
        ).to(device)

        self.critic_target = MLP(
            input_size=self.state_dim + self.action_dim,
            output_size=1,
            hidden_sizes=self.network_cfg.hidden_sizes_critic,
        ).to(device)
        self.critic_target.load_state_dict(self.critic.state_dict())

        # create optimizer
        self.actor_optim = optim.Adam(
            self.actor.parameters(),
            lr=self.optim_cfg.lr_actor,
            weight_decay=self.optim_cfg.weight_decay,
        )

        self.critic_optim = optim.Adam(
            self.critic.parameters(),
            lr=self.optim_cfg.lr_critic,
            weight_decay=self.optim_cfg.weight_decay,
        )

        # load the optimizer and model parameters
        if self.args.load_from is not None and os.path.exists(self.args.load_from):
            self.load_params(self.args.load_from)

    def _initialize(self):
        """Initialize non-common things."""
        # create network
        self._init_network()

        if not self.args.test:
            # replay memory
            self.memory = ReplayBuffer(self.buffer_size, self.batch_size)

    def select_action(self, state: np.ndarray) -> np.ndarray:
        """Select an action from the input space."""
        self.curr_state = state
        state = self._preprocess_state(state)

        # if initial random action should be conducted
        if self.total_step < self.initial_random_action and not self.args.test:
            return np.array(self.env.action_space.sample())

        selected_action = self.actor(state).detach().cpu().numpy()

        if not self.args.test:
            noise = self.noise.sample()
            selected_action = np.clip(selected_action + noise, -1.0, 1.0)

        return selected_action

    # pylint: disable=no-self-use
    def _preprocess_state(self, state: np.ndarray) -> torch.Tensor:
        """Preprocess state so that actor selects an action."""
        state = torch.FloatTensor(state).to(device)
        return state

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, np.float64, bool, dict]:
        """Take an action and return the response of the env."""
        next_state, reward, done, info = self.env.step(action)

        if not self.args.test:
            # if the last state is not a terminal state, store done as false
            done_bool = (
                False if self.episode_step == self.args.max_episode_steps else done
            )
            transition = (self.curr_state, action, reward, next_state, done_bool)
            self._add_transition_to_memory(transition)

        return next_state, reward, done, info

    def _add_transition_to_memory(self, transition: Tuple[np.ndarray, ...]):
        """Add 1 step and n step transitions to memory."""
        self.memory.add(transition)

    def update_model(self) -> Tuple[torch.Tensor, torch.Tensor]:
        """Train the model after each episode."""
        experiences = self.memory.sample()
        states, actions, rewards, next_states, dones = experiences

        # G_t   = r + gamma * v(s_{t+1})  if state != Terminal
        #       = r                       otherwise
        masks = 1 - dones
        next_actions = self.actor_target(next_states)
        next_values = self.critic_target(torch.cat((next_states, next_actions), dim=-1))
        curr_returns = rewards + self.gamma * next_values * masks
        curr_returns = curr_returns.to(device)

        # train critic
        values = self.critic(torch.cat((states, actions), dim=-1))
        critic_loss = F.mse_loss(values, curr_returns)
        self.critic_optim.zero_grad()
        critic_loss.backward()
        nn.utils.clip_grad_norm_(self.critic.parameters(), self.gradient_clip_cr)
        self.critic_optim.step()

        # train actor
        actions = self.actor(states)
        actor_loss = -self.critic(torch.cat((states, actions), dim=-1)).mean()
        self.actor_optim.zero_grad()
        actor_loss.backward()
        nn.utils.clip_grad_norm_(self.actor.parameters(), self.gradient_clip_ac)
        self.actor_optim.step()

        # update target networks
        common_utils.soft_update(self.actor, self.actor_target, self.tau)
        common_utils.soft_update(self.critic, self.critic_target, self.tau)

        return actor_loss.item(), critic_loss.item()

    def load_params(self, path: str):
        """Load model and optimizer parameters."""
        if not os.path.exists(path):
            print("[ERROR] the input path does not exist. ->", path)
            return

        params = torch.load(path)
        self.actor.load_state_dict(params["actor_state_dict"])
        self.actor_target.load_state_dict(params["actor_target_state_dict"])
        self.critic.load_state_dict(params["critic_state_dict"])
        self.critic_target.load_state_dict(params["critic_target_state_dict"])
        self.actor_optim.load_state_dict(params["actor_optim_state_dict"])
        self.critic_optim.load_state_dict(params["critic_optim_state_dict"])
        print("[INFO] loaded the model and optimizer from", path)

    def save_params(self, n_episode: int):
        """Save model and optimizer parameters."""
        params = {
            "actor_state_dict": self.actor.state_dict(),
            "actor_target_state_dict": self.actor_target.state_dict(),
            "critic_state_dict": self.critic.state_dict(),
            "critic_target_state_dict": self.critic_target.state_dict(),
            "actor_optim_state_dict": self.actor_optim.state_dict(),
            "critic_optim_state_dict": self.critic_optim.state_dict(),
        }
        Agent.save_params(self, params, n_episode)

    def write_log(self, i: int, loss: np.ndarray, score: int, avg_time_cost: float):
        """Write log about loss and score"""
        total_loss = loss.sum()

        print(
            "[INFO] episode %d, episode step: %d, total step: %d, total score: %d\n"
            "total loss: %f actor_loss: %.3f critic_loss: %.3f (spent %.6f sec/step)\n"
            % (
                i,
                self.episode_step,
                self.total_step,
                score,
                total_loss,
                loss[0],
                loss[1],
                avg_time_cost,
            )  # actor loss  # critic loss
        )

        if self.args.log:
            wandb.log(
                {
                    "score": score,
                    "total loss": total_loss,
                    "actor loss": loss[0],
                    "critic loss": loss[1],
                    "time per each step": avg_time_cost,
                }
            )

    # pylint: disable=no-self-use, unnecessary-pass
    def pretrain(self):
        """Pretraining steps."""
        pass

    def train(self):
        """Train the agent."""
        # logger
        if self.args.log:
            self.set_wandb(is_training=True)
            # wandb.watch([self.actor, self.critic], log="parameters")

        # pre-training if needed
        self.pretrain()

        for self.i_episode in range(1, self.args.episode_num + 1):
            state = self.env.reset()
            done = False
            score = 0
            self.episode_step = 0
            losses = list()

            t_begin = time.time()

            while not done:
                if self.args.render and self.i_episode >= self.args.render_after:
                    self.env.render()

                action = self.select_action(state)
                next_state, reward, done, _ = self.step(action)
                self.total_step += 1
                self.episode_step += 1

                if len(self.memory) >= self.batch_size:
                    for _ in range(self.multiple_learn):
                        loss = self.update_model()
                        losses.append(loss)  # for logging

                state = next_state
                score += reward

            t_end = time.time()
            avg_time_cost = (t_end - t_begin) / self.episode_step

            # logging
            if losses:
                avg_loss = np.vstack(losses).mean(axis=0)
                self.write_log(self.i_episode, avg_loss, score, avg_time_cost)
                losses.clear()

            if self.i_episode % self.args.save_period == 0:
                self.save_params(self.i_episode)
                self.interim_test()

        # termination
        self.env.close()
        self.save_params(self.i_episode)
        self.interim_test()
