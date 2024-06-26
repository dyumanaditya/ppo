import gym

from isaac_ppo.scripts.ppo import PPO
from isaac_ppo.scripts.hyperparameters import Hyperparameters


def main():
	env_name = 'BipedalWalker-v3'
	learn = False
	render = False

	if not learn:
		render = True

	if render:
		env = gym.make(env_name, render_mode='human')
	else:
		env = gym.make(env_name, render_mode=None)

	# Create the hyperparameters object
	hyperparameters = Hyperparameters()
	hyperparameters.render = render

	agent = PPO(env, hyperparameters)
	if learn:
		agent.learn(max_steps=1000000)
	else:
		agent.simulate('policies/saved/ppo_actor_critic_1000.pth')


if __name__ == '__main__':
	main()
