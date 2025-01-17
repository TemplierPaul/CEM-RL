# !/usr/bin/env python3
import argparse
from copy import deepcopy

import gym
import gym.spaces
import numpy as np
import pandas as pd
import torch.multiprocessing as mp

from ddpg import D3PG
from td3 import DTD3
from random_process import *
from util import *
from memory import Memory, SharedMemory

USE_CUDA = torch.cuda.is_available()
if USE_CUDA:
    FloatTensor = torch.cuda.FloatTensor
else:
    FloatTensor = torch.FloatTensor

import wandb
from tqdm import tqdm


def evaluate(actor, env, memory=None, n_episodes=1, random=False, noise=None, render=False, seed=-1):
    """
    Computes the score of an actor on a given number of runs
    """

    if not random:
        def policy(state):
            state = FloatTensor(state.reshape(-1))
            action = actor(state).cpu().data.numpy().flatten()

            if noise is not None:
                action += noise.sample()

            return np.clip(action, -max_action, max_action)

    else:
        def policy(state):
            return env.action_space.sample()

    scores = []
    steps = 0

    for ep in range(n_episodes):
        if args.debug:
            print(f"\nStarting evaluation episode {ep}")

        score = 0
        ep_steps = 0
        if seed >= 0:
            obs, _ = env.reset(seed=seed)
        else:
            obs, _ = env.reset()
        obs = deepcopy(obs)
        done_bool = False
        print(f"Max frames: {env._max_episode_steps}")

        while not done_bool:

            # get next action and act
            action = policy(obs)
            n_obs, reward, done, _, info = env.step(action)
            # done_bool = 1 if steps + \
            #     1 == env._max_episode_steps else float(done)
            ep_steps += 1
            done_bool = 1 if ep_steps == env._max_episode_steps else float(done)
            score += reward
            steps += 1

            # adding in memory
            if memory is not None:
                memory.add((obs, n_obs, action, reward, done_bool))
            obs = n_obs

            # render if needed
            if render:
                env.render()

            # reset when done
            # if done_bool:
            #     print("RESET")
            #     if seed >= 0:
            #         env.reset(seed=seed)
            #     else:
            #         env.reset()
            if args.debug: print(f"steps: {steps}", end="\r")

        scores.append(score)

    return np.mean(scores), steps


def train(n_episodes, output=None, debug=False, render=False, seed=-1):
    """
    Train the whole process
    """

    total_steps = 0
    step_cpt = 0
    n = 0

    if args.wandb != "":
        entity, project = args.wandb.split("/")
        wandb_run = wandb.init(project=project, entity=entity, config=args)
        print("wandb run:", wandb.run.name)

    bar = tqdm(total=args.max_steps)
    if debug: print("Starting training...")
    while total_steps < args.max_steps:

        random = total_steps < args.start_steps
        actor_steps = 0

        # training all agents
        for i in range(args.n_actor):
            if debug: print(f"Evaluating actor {i}")
            f, s = evaluate(agent.actors[i], envs[i], n_episodes=n_episodes,
                            noise=a_noise, random=random, memory=memory, render=render, seed=seed)
            actor_steps += s
            total_steps += s
            step_cpt += s

            # print score
            prCyan(f'noisy RL agent fitness:{f}')

        for i in range(args.n_actor):
            # if debug: print(f"Training actor {i}")
            agent.train(actor_steps, i)

        # saving models and scores
        # if step_cpt >= args.period:
        if debug: print("Logging")

        step_cpt = 0

        fs = []
        for i in range(args.n_actor):
            f, _ = evaluate(
                agent.actors[i], envs[i], n_episodes=args.n_eval, seed=seed)
            fs.append(f)

            # print score
            prRed(f'RL agent fitness:{f}')

        # saving scores
        res = {"total_steps": total_steps,
                "average_score": np.mean(fs), "best_score": np.max(fs)}
        # print(f"Saving results: {res}")

        bar.set_description(f"Iteration#{n} - {total_steps} steps - Average score: {np.mean(fs)}")
        bar.update(actor_steps)

        for i in range(args.n_actor):
            res[f"score_{i}"] = fs[i]

        if args.wandb != "":
            wandb_run.log(res)

        n += 1

        # printing iteration resume
        if debug:
            prPurple(f'Iteration#{n}: Total steps:{total_steps} \n')

    if debug: print("Training done")


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument('--mode', default='train', type=str,)
    parser.add_argument('--env', default='HalfCheetah-v2', type=str)
    parser.add_argument('--start_steps', default=10000, type=int)

    # DDPG parameters
    parser.add_argument('--actor_lr', default=0.001, type=float)
    parser.add_argument('--critic_lr', default=0.001, type=float)
    parser.add_argument('--batch_size', default=100, type=int)
    parser.add_argument('--discount', default=0.99, type=float)
    parser.add_argument('--reward_scale', default=1., type=float)
    parser.add_argument('--tau', default=0.005, type=float)
    parser.add_argument('--layer_norm', dest='layer_norm', action='store_true')

    # TD3 parameters
    parser.add_argument('--use_td3', dest='use_td3', action='store_true')
    parser.add_argument('--policy_noise', default=0.2, type=float)
    parser.add_argument('--noise_clip', default=0.5, type=float)
    parser.add_argument('--policy_freq', default=2, type=int)

    # Gaussian noise parameters
    parser.add_argument('--gauss_sigma', default=0.1, type=float)

    # OU process parameters
    parser.add_argument('--ou_noise', dest='ou_noise', action='store_true')
    parser.add_argument('--ou_theta', default=0.15, type=float)
    parser.add_argument('--ou_sigma', default=0.2, type=float)
    parser.add_argument('--ou_mu', default=0.0, type=float)

    # Parameter noise parameters
    parser.add_argument('--param_init_std', default=0.01, type=float)
    parser.add_argument('--param_scale', default=0.2, type=float)
    parser.add_argument('--param_adapt', default=1.01, type=float)

    # Training parameters
    parser.add_argument('--n_actor', default=1, type=int)
    parser.add_argument('--n_episodes', default=1, type=int)
    parser.add_argument('--n_eval', default=10, type=int)
    parser.add_argument('--period', default=5000, type=int)
    parser.add_argument('--max_steps', default=1000000, type=int)
    parser.add_argument('--mem_size', default=1000000, type=int)

    # Testing parameters
    parser.add_argument('--filename', default="", type=str)
    parser.add_argument('--n_test', default=1, type=int)

    # misc
    parser.add_argument('--wandb', default='', type=str)
    parser.add_argument('--output', default='results', type=str)
    parser.add_argument('--save_all_models',
                        dest="save_all_models", action="store_true")
    parser.add_argument('--debug', dest='debug', action='store_true')
    parser.add_argument('--seed', default=-1, type=int)
    parser.add_argument('--render', dest='render', action='store_true')

    args = parser.parse_args()
    # args.output = get_output_folder(args.output, args.env)

    # with open(args.output + "/parameters.txt", 'w') as file:
    #     for key, value in vars(args).items():
    #         file.write("{} = {}\n".format(key, value))

    # The environment
    envs = [gym.make(args.env) for _ in range(args.n_actor)]
    state_dim = envs[0].observation_space.shape[0]
    action_dim = envs[0].action_space.shape[0]
    max_action = int(envs[0].action_space.high[0])

    # Random seed
    if args.seed > 0:
        np.random.seed(args.seed)
        # for j in range(args.n_actor):
            # envs[j].seed(args.seed)
        torch.manual_seed(args.seed)

    # replay buffer
    memory = Memory(args.mem_size, state_dim, action_dim)

    # action noise
    if args.ou_noise:
        a_noise = OrnsteinUhlenbeckProcess(
            action_dim, mu=args.ou_mu, theta=args.ou_theta, sigma=args.ou_sigma)
    else:
        a_noise = GaussianNoise(action_dim, sigma=args.gauss_sigma)

    # agent
    if args.use_td3:
        agent = DTD3(state_dim, action_dim, max_action, memory, args)
    else:
        agent = D3PG(state_dim, action_dim, max_action, memory, args)

    if args.mode == 'train':
        train(n_episodes=args.n_episodes,
              output=args.output, debug=args.debug, render=args.render, seed=args.seed)

    else:
        raise RuntimeError('undefined mode {}'.format(args.mode))
