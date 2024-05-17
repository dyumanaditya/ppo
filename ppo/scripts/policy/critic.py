import torch
import torch.nn as nn

from ppo.scripts.policy.network import MLP


class Critic(nn.Module):
	def __init__(self, state_dim, hidden_sizes, activations, device):
		super(Critic, self).__init__()
		self.device = device

		self.v_net = MLP(state_dim, 1, hidden_sizes, activations).to(self.device)

	def forward(self, state):
		value = self.v_net.mlp(state)
		return value

	def get_optimizer(self, optimizer, lr):
		if optimizer == 'Adam':
			return torch.optim.Adam(self.parameters(), lr=lr)
		elif optimizer == 'RMSprop':
			return torch.optim.RMSprop(self.parameters(), lr=lr)
		elif optimizer == 'SGD':
			return torch.optim.SGD(self.parameters(), lr=lr)
		else:
			raise ValueError("Invalid Critic optimizer")
