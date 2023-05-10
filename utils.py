import logging
import random
import time
from collections import deque, namedtuple

import gymnasium as gym
import numpy as np
import torch
import torchvision.transforms as T
from gymnasium.core import Env
from gymnasium.wrappers import AtariPreprocessing, FrameStack
from torch.utils.data import Dataset

logging.basicConfig(format=(
        "[%(levelname)s:%(asctime)s] " "%(message)s"), level=logging.INFO)

Transition = namedtuple('Transition',
                        ('state', 'action', 'next_state', 'reward', 'done'))

SEED = 1122

def get_device():
    if torch.cuda.is_available():
        logging.info('************* Using CUDA *************')
        return torch.device('cuda')
    elif torch.has_mps:
        logging.info('************* Using MPS *************')
        return torch.device('mps')
    else:
        logging.info('************* Using CPU *************')
        return torch.device('cpu')

def preprocess_observation(obs, mode='simple', new_size=(84, 84)):
    """
    obs - a 210 x 160 x 3 ndarray representing an atari frame
    returns:
      a 3 x 210 x 160 normalized pytorch tensor
    """
    if mode == 'simple':
        return torch.from_numpy(obs).permute(2, 0, 1)/255.0
    elif mode == 'resize':
        image_tensor = torch.tensor(obs).float()
        image_tensor = image_tensor.permute(2, 0, 1)
        # Resize image using torch.nn.functional.interpolate
        image_tensor = image_tensor.unsqueeze(0) # Add a batch dimension
        transform = T.Compose([
            T.Grayscale(),              # Convert to grayscale to save memory
            T.Resize(new_size, interpolation=T.InterpolationMode.BILINEAR, antialias=True)  # resize smaller to save memory
        ])
        resized_image_tensor = transform(image_tensor)
        resized_image_tensor = resized_image_tensor.squeeze() # Remove the extra dimension
        resized_image_tensor /= 255.0   # Normalize the pixel values to [0, 1] range

        return resized_image_tensor.unsqueeze(0)

def validate(model, args, render:bool=False, nepisodes=5, wandb=False, mode='simple'):
    """
    Evaluates the performance of the given agent on the Ms. Pac-Man Atari game using a specified number of episodes, and returns the average reward and number of steps taken per episode.

    Parameters
    ----------
    model : object
        A reinforcement learning model object with a `get_action` method.
    args : argparse.Namespace
        argumetns passed in from command line
    render : bool, optional
        Whether to render the game during validation. Defaults to False.
    nepisodes : int, optional
        The number of episodes to play for evaluation. Defaults to 5.
    wandb : bool, optional
        Whether to log results to Weights & Biases. Defaults to False.
    mode : str, optional
        The image preprocessing mode to use before feeding image to the model. Can be 'simple' or 'resize'. Defaults to 'simple'.
    """
    assert hasattr(model, "get_action")
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    # NOTE: seed for python's random library is set in train.py, it's not set in here since this function will be called
    # during training, and I want to avoid to setting seeds since that may create patterns in samples generated in experience replay
    
    model.eval()        # turn into eval mode
    if render:
        nepisodes = 1       # only render one episode if render is True

    steps_alive, reward_arr = [], []        # to store each episode's reward and steps taken
    for i in range(nepisodes):
        logging.info(f"Validating episode {i+1}...")
        render_mode = "human"  if render else None
        env = gym.make(args.env, render_mode=render_mode)      # NOTE: modify render functionality for better graphics
        obs = env.reset(seed=SEED+i)[0]       # use a different seed for each separate episode
        
        observation = preprocess_observation(obs, mode=mode).unsqueeze(0).unsqueeze(0)      # 1 x 1 x ic x iH x iW
        prev_state = None
        step, ep_total_reward, done = 0, 0, False
        # play until the agent dies or we exceed 50000 observations
        while not done and step < 50000:
            action, prev_state = model.get_action(observation, prev_state)
            env_output = env.step(action)
            ep_total_reward += env_output[1]
            done = env_output[2]
            observation = preprocess_observation(env_output[0], mode=mode).unsqueeze(0).unsqueeze(0)
            step += 1
        steps_alive.append(step)
        reward_arr.append(ep_total_reward)
    
    if wandb:           # log into wandb if using it
        wandb.log({"Mean Reward (Validation)": np.mean(reward_arr),
                   'std Reward (Validation)': np.std(reward_arr)})
    
    logging.info(f"{'-'*10} BEGIN VALIDATION {'-'*10}")
    logging.info("Steps taken over each of {:d} episodes: {}".format(
        nepisodes, ", ".join(str(step) for step in steps_alive)))
    logging.info("Total return after {:d} episodes: {:.3f}".format(nepisodes, np.sum(reward_arr)))
    logging.info(f"Mean return for each episode: {np.mean(reward_arr):.3f}, (std: {np.std(reward_arr):.3f})")
    logging.info(f"{'-'*10} END VALIDATION {'-'*10}")


class ReplayBuffer:
    '''a simple replay buffer implemented using deque'''
    def __init__(self, size: int) -> None:
        self.memory = deque([], maxlen=size)
    
    def push(self, transition: Transition):
        '''add transitions to the replay buffer'''
        self.memory.append(Transition(*transition))
    
    def sample(self, bsz: int):
        '''sample bsz number of transitions from the replay buffer'''
        return random.sample(self.memory, bsz)
    
    def __len__(self):
        return len(self.memory)


class SkipFrameWrapper(gym.Wrapper):
    '''
    Wrapper to skip frames (which is same as performing the same action for multiple frames)
    This has two advantages:
    - make more efficient use of frames, since most frames are redundant
    - reduce computation need for forward pass, since we only need one forward pass (to decide an action) to play four times
      (whereas doing four forward passes to decide separate actions need more computation)
    '''
    def __init__(self, env: Env, skip: int = 4):
        super().__init__(env)
        self.skip = skip
    
    def step(self, action):
        total_reward = 0.0
        done = False
        for _ in range(self.skip):
            env_output = self.env.step(action)
            total_reward += env_output[1]
            done = env_output[2]
            obs = env_output[0]
            if done:
                break
        return obs, total_reward, done


class AtariGameEnv(gym.Wrapper):
    def __init__(self, env_name: str, num_stack_frame: int=4, terminal_on_life_loss: bool=True) -> None:
        assert 'v5' in env_name, 'Please use envs with v5!'
        env = gym.make(env_name, frameskip=1, repeat_action_probability=0.0)                # frame skip is done in next step, don't repeat actions
        env = AtariPreprocessing(env, scale_obs=True, terminal_on_life_loss=terminal_on_life_loss)           # normalize and rescale, end episode if life lost
        env = FrameStack(env, num_stack_frame)
        super().__init__(env)
        
    def __preprocess_output(self, env_output: tuple) -> tuple:
        obs = env_output[0]._frames
        obs = torch.tensor(obs)
        env_output = list(env_output)
        env_output[0] = obs
        
        return tuple(env_output)
    
    def reset(self, seed: int=None) -> tuple:
        env_output = self.env.reset(seed=seed)
        env_output = self.__preprocess_output(env_output)
        
        return env_output
    
    def step(self, action: int) -> tuple:
        env_output = self.env.step(action)
        env_output = self.__preprocess_output(env_output)
        
        return env_output


class TrajectorySamples(Dataset):
    def __init__(self, **kwargs) -> None:
        super().__init__()
        self.old_probs = kwargs["old_probs"]
        self.observations_arr = kwargs["observations_arr"]
        self.advantages = kwargs["advantages"]
        self.vtarget_arr = kwargs["vtarget_arr"]
        self.actions_arr = kwargs["actions_arr"]
        assert len(self.old_probs) == len(self.observations_arr) == len(self.advantages) == len(self.vtarget_arr) == len(self.actions_arr)
    
    def __len__(self):
        return len(self.old_probs)
    
    def __getitem__(self, index) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        return self.old_probs[index], self.observations_arr[index], self.advantages[index], self.vtarget_arr[index], self.actions_arr[index]
        