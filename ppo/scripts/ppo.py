import torch
import time

from ppo.scripts.memory import Memory
from ppo.scripts.policy.actor_critic import ActorCritic
from ppo.scripts.hyperparameters import Hyperparameters


class PPO:
	def __init__(self, env, hyperparameters=None, save_freq=20):
		self.hyperparameters = hyperparameters
		self.save_freq = save_freq
		self.env = env
		self.render = self.hyperparameters.render
		self.verbose = True

		if self.hyperparameters is None:
			self.hyperparameters = Hyperparameters()

		# Every PPO instance is given a timestamp to save all related files
		self.timestamp = time.strftime('%d-%m-%Y__%H-%M-%S')

		# Set the device
		gpu = 0
		self.compute = self.hyperparameters.device
		if self.compute == 'gpu':
			self.device = torch.device(f"cuda:{gpu}" if torch.cuda.is_available() else "cpu")
		else:
			self.device = torch.device("cpu")
		print(f"Using device: {self.device}")

		# Isaac sim gives us observations in the form of a dictionary
		self.observation_space = env.observation_space['policy'].shape[1]
		self.action_space = env.action_space.shape[1]

		# Number of environments we run in parallel
		self.num_envs = env.observation_space['policy'].shape[0]

		# Where to save policies
		self.log_dir = '.'

		# Make hyperparameters class attributes
		self.gamma = self.hyperparameters.gamma
		self.lam = self.hyperparameters.lam

		self.lr = self.hyperparameters.lr

		self.clip_ratio = self.hyperparameters.clip_ratio
		self.kl_target = self.hyperparameters.kl_target

		self.value_loss_coef = self.hyperparameters.value_loss_coef
		self.entropy_coef = self.hyperparameters.entropy_coef
		self.max_grad_norm = self.hyperparameters.max_grad_norm
		self.clip_value_loss = self.hyperparameters.clip_value_loss

		self.actor_hidden_sizes = self.hyperparameters.actor_hidden_sizes
		self.critic_hidden_sizes = self.hyperparameters.critic_hidden_sizes
		self.actor_activations = self.hyperparameters.actor_activations
		self.critic_activations = self.hyperparameters.critic_activations

		self.optimizer = self.hyperparameters.optimizer

		# Initialize the actor and critic networks
		self.actor_critic = ActorCritic(self.observation_space, self.action_space, self.actor_hidden_sizes, self.critic_hidden_sizes, self.actor_activations, self.critic_activations, self.device).to(self.device)

		# Initialize the actor and critic optimizers
		self.actor_critic_optimizer = self.actor_critic.get_optimizer(self.optimizer, self.lr)

		# Iterations
		self.num_epochs = self.hyperparameters.num_epochs
		self.num_minibatches = self.hyperparameters.num_minibatches

		# Initialize memory
		self.num_transitions_per_env = self.hyperparameters.num_transitions_per_env
		self.memory = Memory(self.observation_space, self.action_space, self.num_envs, self.num_transitions_per_env, self.device, self.gamma, self.lam)

	def learn(self, max_steps=100000, actor_critic_model_path=None):
		# Reset the environment
		states, _ = self.env.reset()
		loss = 0

		# Load the model if specified
		if actor_critic_model_path is not None:
			self.actor_critic.load_state_dict(torch.load(actor_critic_model_path))

		total_episodes = 0
		env_steps = 0
		for timestep in range(max_steps):
			# Episode related information
			num_rollout_episodes = 0
			episode_length = torch.zeros(self.num_envs, device=self.device)
			episode_rewards = torch.zeros(self.num_envs, device=self.device)
			cumulative_episode_rewards = 0
			cumulative_episode_lengths = 0

			# Collect rollouts
			with torch.inference_mode():
				for rollout in range(self.num_transitions_per_env):
					if self.render:
						self.env.render()

					# Get the action from the actor and take it
					# State is in the form of a dictionary
					states = states['policy']
					actions = self.actor_critic.get_action(states)
					next_states, rewards, dones, timeouts, info = self.env.step(actions)
					
					# Store the transition
					# Get value and log probs
					values = self.actor_critic.get_value(states)
					log_probs = self.actor_critic.log_prob_from_distribution(actions)

					# Get the mean and std for KL calculations later on
					mu, sigma = self.actor_critic.get_mu_sigma()
					self._process_env_step(states, actions, rewards, dones, timeouts, values, log_probs, mu, sigma)

					# Set to new observation
					states = next_states

					# Keep track of rewards to print later
					num_rollout_episodes += torch.sum(dones).item()
					total_episodes += num_rollout_episodes
					episode_rewards += rewards
					episode_length += 1

					# If any episode finished, add to cumulative rewards
					new_ids = (dones > 0).nonzero(as_tuple=False)
					cumulative_episode_rewards += torch.sum(episode_rewards[new_ids]).item()
					cumulative_episode_lengths += torch.sum(episode_length[new_ids]).item()
					episode_rewards[new_ids] = 0
					episode_length[new_ids] = 0

					# If any episode timed out or terminated, clear the reward and length
					new_ids_timeout = (timeouts > 0).nonzero(as_tuple=False)
					episode_rewards[new_ids_timeout] = 0
					episode_length[new_ids_timeout] = 0

				# Compute the returns for the rollout
				self.memory.compute_returns(values)

			# Go over the rollouts for multiple epochs
			mean_loss = 0
			mean_value_loss = 0
			mean_surrogate_loss = 0
			for epoch in range(self.num_epochs):
				# Go over each of the mini batches
				for rollout_minibatch in self.memory.get_minibatches(self.num_minibatches):
					# Update the actor and critic
					loss, value_loss, surrogate_loss = self._update_actor_critic(rollout_minibatch)
					mean_loss += loss
					mean_value_loss += value_loss
					mean_surrogate_loss += surrogate_loss

			num_updates = self.num_epochs * self.num_minibatches
			mean_loss /= num_updates
			mean_value_loss /= num_updates
			mean_surrogate_loss /= num_updates

			# Reset the memory
			self.memory.reset()

			# Print Information
			if self.verbose:
				print("-" * 50)
				print(f"Total steps: {timestep}", flush=True)
				print(f"Total env steps: {env_steps}", flush=True)
				print(f"Total episodes: {total_episodes}", flush=True)
				print("--")
				if num_rollout_episodes == 0:
					mean_episode_reward = torch.sum(episode_rewards).item()
					mean_episode_length = torch.sum(episode_length).item()
				else:
					mean_episode_reward = cumulative_episode_rewards / num_rollout_episodes
					mean_episode_length = cumulative_episode_lengths / num_rollout_episodes
				print(f"Mean Episode Reward: {round(mean_episode_reward, 10)}", flush=True)
				print(f"Mean Episode Length: {round(mean_episode_length, 10)}", flush=True)
				print("--")
				print(f"Mean Loss: {mean_loss}", flush=True)
				print(f"Mean Value Loss: {mean_value_loss}", flush=True)
				print(f"Mean Surrogate Loss: {mean_surrogate_loss}", flush=True)
				print("--")
				print(f"Learning Rate: {self.lr}")
				print("-" * 50)

			# Save the model
			if timestep % self.save_freq == 0:
				torch.save(self.actor_critic.state_dict(), f"{self.log_dir}/ppo_actor_critic_{env_steps}.pth")

			env_steps += self.num_transitions_per_env * self.num_envs

		# End
		self.env.close()

	def simulate(self, actor_critic_model_path, max_episodes=20):
		# Load the model
		self.actor_critic.load_state_dict(torch.load(actor_critic_model_path))

		# Reset the environment
		states, _ = self.env.reset()

		# Episode related information
		episode_counter = 0
		episode_length = torch.zeros(self.num_envs, device=self.device)
		episode_rewards = torch.zeros(self.num_envs, device=self.device)
		cumulative_episode_rewards = 0
		cumulative_episode_lengths = 0

		# Simulate the environment
		while episode_counter <= max_episodes:
			if self.render:
				self.env.render()

			with torch.inference_mode():
				# Get the action from the actor and take it
				# State is in the form of a dictionary
				states = states['policy']
				actions = self.actor_critic.get_action(states)
				next_states, rewards, dones, timeouts, info = self.env.step(actions)

				states = next_states

				# Update the episode information
				episode_counter += torch.sum(dones).item()
				episode_rewards += rewards
				episode_length += 1

				new_ids = (dones > 0).nonzero(as_tuple=False)
				cumulative_episode_rewards += torch.sum(episode_rewards[new_ids]).item()
				cumulative_episode_lengths += torch.sum(episode_length[new_ids]).item()
				episode_rewards[new_ids] = 0
				episode_length[new_ids] = 0

				# If any episode timed out or terminated, clear the reward and length
				new_ids_timeout = (timeouts > 0).nonzero(as_tuple=False)
				episode_rewards[new_ids_timeout] = 0
				episode_length[new_ids_timeout] = 0

				if episode_counter == 0:
					mean_episode_reward = torch.sum(episode_rewards).item()
					mean_episode_length = torch.sum(episode_length).item()
				else:
					mean_episode_reward = cumulative_episode_rewards / episode_counter
					mean_episode_length = cumulative_episode_lengths / episode_counter
				print("-"*50)
				print(f"Mean Episode Reward: {round(mean_episode_reward, 10)}", flush=True)
				print(f"Mean Episode Length: {round(mean_episode_length, 10)}", flush=True)
				print("-"*50)

		# End
		self.env.close()

	def save(self, path):
		torch.save(self.actor_critic.state_dict(), path + '.pth')

	def load(self, path):
		self.actor_critic.load_state_dict(torch.load(path + '.pth'))

	def _update_actor_critic(self, minibatch):
		# Extract values from minibatch
		states_batch, actions_batch, returns_batch, values_batch, advantages_batch, log_probs_batch, mu_batch, sigma_batch = minibatch

		# Get the policy and value and update the action distribution
		_, current_log_probs, current_values = self.actor_critic(states_batch, actions_batch)

		# Get the current policy mean and std from the updated distribution
		current_mu, current_sigma = self.actor_critic.get_mu_sigma()

		# Get the entropy
		current_entropy = self.actor_critic.get_entropy()

		# Adaptive Learning rate based on KL Divergence
		if self.kl_target is not None:
			with torch.inference_mode():
				# KL = sum[p_new * log(p_new / p_old)] = sum[log(sigma_new / sigma_old) + (sigma_old^2 + (mu_old - mu_new)^2) / (2 * sigma_new^2) - 0.5]
				kl = torch.sum(
					torch.log(current_sigma / sigma_batch + 1.0e-5)
					+ (torch.square(sigma_batch) + torch.square(mu_batch - current_mu)) / (2.0 * torch.square(current_sigma))
					- 0.5,
					dim=-1
				)
				kl_mean = kl.mean()

				# If KL is too high, reduce the learning rate
				# If KL is too low, increase the learning rate
				if kl_mean > 2.0 * self.kl_target:
					self.lr = max(1e-5, self.lr / 1.5)
				elif self.kl_target / 2.0 > kl_mean > 0.0:
					self.lr = min(1e-2, self.lr * 1.5)

				# Adjust the learning rate
				for param_group in self.actor_critic_optimizer.param_groups:
					param_group['lr'] = self.lr

		# Compute loss
		# Policy Loss
		ratio = torch.exp(current_log_probs - torch.squeeze(log_probs_batch))
		surrogate = -torch.squeeze(advantages_batch) * ratio
		clipped_surrogate = -torch.squeeze(advantages_batch) * torch.clamp(ratio, 1 - self.clip_ratio, 1 + self.clip_ratio)
		surrogate_loss = -torch.min(surrogate, clipped_surrogate).mean()

		# Value Loss
		if self.clip_value_loss:
			value_clipped = values_batch + (current_values - values_batch).clamp(-self.clip_ratio, self.clip_ratio)
			value_losses = (current_values - returns_batch).pow(2)
			value_losses_clipped = (value_clipped - returns_batch).pow(2)
			value_loss = torch.max(value_losses, value_losses_clipped).mean()
		else:
			value_loss = (current_values - returns_batch).pow(2).mean()

		# Total Loss
		loss = surrogate_loss + self.value_loss_coef * value_loss - self.entropy_coef * current_entropy.mean()

		# Gradient step
		self.actor_critic_optimizer.zero_grad()
		loss.backward()
		torch.nn.utils.clip_grad_norm_(self.actor_critic.parameters(), self.max_grad_norm)
		self.actor_critic_optimizer.step()
		return loss.item(), value_loss.item(), surrogate_loss.item()

	def _process_env_step(self, states, actions, rewards, dones, timeouts, values, log_probs, mu, sigma):
		# Bootstrap on timeouts
		rewards += self.gamma * torch.squeeze(values * timeouts.unsqueeze(dim=1), dim=1)

		# Store the transition
		self.memory.store_transitions(states, actions, rewards, dones, values, log_probs, mu, sigma)
