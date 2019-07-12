#!/usr/bin/env python3

import sys
import os
import pickle
import json
import math
import argparse
import random

import collections
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from tensorboardX import SummaryWriter

HIDDEN_SIZE = 512
N_LAYERS = 3

class OurDataset(Dataset):
	def __init__(self, data, labels, categories):
		# assert not np.isnan(data).any(), "datum is nan: {}".format(data)
		# assert not np.isnan(labels).any(), "labels is nan: {}".format(labels)
		self.data = data
		self.labels = labels
		self.categories = categories
		assert(len(self.data) == len(self.labels) == len(self.categories))

	def __getitem__(self, index):
		data, labels, categories = torch.FloatTensor(self.data[index]), torch.FloatTensor(self.labels[index]), torch.FloatTensor(self.categories[index])
		return data, labels, categories

	def __len__(self):
		return len(self.data)

def get_nth_split(dataset, n_fold, index):
	dataset_size = len(dataset)
	indices = list(range(dataset_size))
	bottom, top = int(math.floor(float(dataset_size)*index/n_fold)), int(math.floor(float(dataset_size)*(index+1)/n_fold))
	train_indices, test_indices = indices[0:bottom]+indices[top:], indices[bottom:top]
	return train_indices, test_indices

class OurLSTMModule(nn.Module):

	def __init__(self, num_inputs, num_outputs, hidden_size, n_layers, batch_size, device, forgetting=False):
		super(OurLSTMModule, self).__init__()
		self.hidden_size = hidden_size
		self.n_layers = n_layers
		self.num_inputs = num_inputs
		self.num_outputs = num_outputs
		self.batch_size = batch_size
		self.device = device
		self.lstm = nn.LSTM(input_size=num_inputs, hidden_size=hidden_size, num_layers=n_layers)
		self.hidden = None
		# self.i2h = nn.Linear(num_inputs, hidden_size)
		self.h2o = nn.Linear(hidden_size, num_outputs)
		# self.softmax = nn.Softmax(dim=2)
		self.forgetting = forgetting

	# batch has seq * batch * input_dim

	def init_hidden(self, batch_size):
		self.hidden = (torch.zeros(self.n_layers, batch_size, self.hidden_size).to(self.device),
		torch.zeros(self.n_layers, self.batch_size, self.hidden_size).to(self.device))

	def forward(self, batch):
		# preprocessed_batch = self.i2h(batch.view(-1,batch.shape[-1])).view(batch.shape[0], batch.shape[1], self.hidden_size)
		# print("batch", batch)
		lstm_out, new_hidden = self.lstm(batch, self.hidden)
		if not self.forgetting:
			self.hidden = new_hidden
		lstm_out, seq_lens = torch.nn.utils.rnn.pad_packed_sequence(lstm_out)
		# output = self.h2o(lstm_out.view(-1, self.hidden_size)).view(*lstm_out.shape[:2], self.num_outputs)
		output = self.h2o(lstm_out)
		# output = self.softmax(output)
		return output, seq_lens

def get_one_hot_vector(class_indices, num_classes, batch_size):
	y_onehot = torch.FloatTensor(batch_size, num_classes)
	y_onehot.zero_()
	return y_onehot.scatter_(1, class_indices.unsqueeze(1), 1)

def custom_collate(seqs):
	# print("seqs", seqs)
	# quit()
	# seqs = [(item[0][:opt.maxLength,:], item[1][:opt.maxLength,:]) for item in seqs]
	seqs, labels, categories = zip(*seqs)
	assert len(seqs) == len(labels) == len(categories)
	seq_lengths = torch.LongTensor([len(seq) for seq in seqs]).to(device)

	# print("seq_lengths", seq_lengths)

	seq_tensor = torch.nn.utils.rnn.pad_sequence(seqs).to(device)
	labels_tensor = torch.nn.utils.rnn.pad_sequence(labels).to(device)
	categories_tensor = torch.nn.utils.rnn.pad_sequence(categories).to(device)

	packed_input = torch.nn.utils.rnn.pack_padded_sequence(seq_tensor, seq_lengths, enforce_sorted=False)
	packed_labels = torch.nn.utils.rnn.pack_padded_sequence(labels_tensor, seq_lengths, enforce_sorted=False)
	packed_categories = torch.nn.utils.rnn.pack_padded_sequence(categories_tensor, seq_lengths, enforce_sorted=False)
	return packed_input, packed_labels, packed_categories
	
def inputonly_collate(seqs):
	# print("seqs", seqs)
	# quit()
	# seqs = [(item[0][:opt.maxLength,:], item[1][:opt.maxLength,:]) for item in seqs]

	seq_lengths = torch.LongTensor([len(seq) for seq in seqs]).to(device)

	# print("seq_lengths", seq_lengths)

	seq_tensor = torch.nn.utils.rnn.pad_sequence(seqs).to(device)
	packed_input = torch.nn.utils.rnn.pack_padded_sequence(seq_tensor, seq_lengths, enforce_sorted=False)
	return packed_input

def train():

	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.train()

	train_indices, _ = get_nth_split(dataset, n_fold, fold)
	train_data = torch.utils.data.Subset(dataset, train_indices)
	train_loader = torch.utils.data.DataLoader(train_data, batch_size=opt.batchSize, shuffle=True, collate_fn=custom_collate)

	optimizer = optim.SGD(lstm_module.parameters(), lr=opt.lr)
	criterion = nn.BCEWithLogitsLoss(reduction="mean")

	writer = SummaryWriter()

	samples = 0
	for i in range(1, sys.maxsize):
		for input_data, labels, _ in train_loader:
			# print("iterating")
			# samples += len(input_data)
			optimizer.zero_grad()
			batch_size = input_data.sorted_indices.shape[0]
			assert batch_size <= opt.batchSize, "batch_size: {}, opt.batchSize: {}".format(batch_size, opt.batchSize)
			lstm_module.init_hidden(batch_size)

			# actual_input = torch.FloatTensor(input_tensor[:,:,:-1]).to(device)

			# print("input_data.data.shape", input_data.data.shape)
			output, seq_lens = lstm_module(input_data)

			# torch.set_printoptions(profile="full")
			# print("output", output.detach().squeeze().transpose(1,0))

			samples += output.shape[1]

			index_tensor = torch.arange(0, output.shape[0], dtype=torch.int64).unsqueeze(1).unsqueeze(2).repeat(1, output.shape[1], output.shape[2])

			selection_tensor = seq_lens.unsqueeze(0).unsqueeze(2).repeat(index_tensor.shape[0], 1, index_tensor.shape[2])-1

			# print("index_tensor.shape", index_tensor.shape, "selection_tensor.shape", selection_tensor.shape)
			mask = (index_tensor <= selection_tensor).byte().to(device)
			mask_exact = (index_tensor == selection_tensor).byte().to(device)
			# torch.set_printoptions(profile="full")
			# print("mask", mask.squeeze())
			labels, _ = torch.nn.utils.rnn.pad_packed_sequence(labels)

			loss = criterion(output[mask].view(-1), labels[mask].view(-1))
			loss.backward()

			optimizer.step()

			# print("masked_output_shape", torch.round(output.detach()[mask]).shape, "masked_labels_shape", labels[mask].shape)
			# print("exact_masked_output_shape", torch.round(output.detach()[mask_exact]).shape, "exact_masked_labels_shape", labels[mask_exact].shape)
			# print("output.shape", output.shape, "labels.shape", labels.shape)
			assert output.shape == labels.shape
			writer.add_scalar("loss", loss.item(), samples)
			sigmoided_output = torch.sigmoid(output.detach())
			accuracy = torch.mean((torch.round(sigmoided_output[mask]) == labels[mask]).float())
			writer.add_scalar("accuracy", accuracy, samples)
			end_accuracy = torch.mean((torch.round(sigmoided_output[mask_exact]) == labels[mask_exact]).float())
			writer.add_scalar("end_accuracy", end_accuracy, samples)

			# confidence_for_correct_one = torch.mean(torch.gather(torch.sigmoid(output.detach()[mask]), 2, labels[mask]))
			# writer.add_scalar("confidence", confidence_for_correct_one, i*opt.batchSize)
			# end_confidence_for_correct_one = torch.mean(torch.gather(torch.sigmoid(output.detach()[-1,:,:]), 1, labels[-1,:].unsqueeze(1)))
			# writer.add_scalar("end_confidence", end_confidence_for_correct_one, i*opt.batchSize)

			not_attack_mask = labels == 0
			confidences = sigmoided_output.detach().clone()
			confidences[not_attack_mask] = 1 - confidences[not_attack_mask]
			writer.add_scalar("confidence", torch.mean(confidences[mask]), samples)
			writer.add_scalar("end_confidence", torch.mean(confidences[mask_exact]), samples)


		# Save after every epoch
		if i % 1 == 0:
			torch.save(lstm_module.state_dict(), '%s/lstm_module_%d.pth' % (writer.log_dir, i))

def test():

	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	test_data = torch.utils.data.Subset(dataset, test_indices)
	test_loader = torch.utils.data.DataLoader(test_data, batch_size=opt.batchSize, shuffle=False, collate_fn=custom_collate)

	all_accuracies = []
	all_end_accuracies = []
	samples = 0

	with open(opt.categoriesMapping, "r") as f:
		categories_mapping_content = json.load(f)
	categories_mapping, mapping = categories_mapping_content["categories_mapping"], categories_mapping_content["mapping"]

	attack_numbers = mapping.values()
	attack_names = mapping.keys()
	category_names = categories_mapping.keys()

	results_by_attack_numbers = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	for index, (input_data, labels, categories) in enumerate(test_loader):

		batch_size = input_data.sorted_indices.shape[0]
		assert batch_size <= opt.batchSize, "batch_size: {}, opt.batchSize: {}".format(batch_size, opt.batchSize)
		lstm_module.init_hidden(batch_size)

		output, seq_lens = lstm_module(input_data)

		samples += output.shape[1]

		index_tensor = torch.arange(0, output.shape[0], dtype=torch.int64).unsqueeze(1).unsqueeze(2).repeat(1, output.shape[1], output.shape[2])

		selection_tensor = seq_lens.unsqueeze(0).unsqueeze(2).repeat(index_tensor.shape[0], 1, index_tensor.shape[2])-1

		mask = (index_tensor <= selection_tensor).byte().to(device)
		mask_exact = (index_tensor == selection_tensor).byte().to(device)

		input_data, _ = torch.nn.utils.rnn.pad_packed_sequence(input_data)
		labels, _ = torch.nn.utils.rnn.pad_packed_sequence(labels)
		categories, _ = torch.nn.utils.rnn.pad_packed_sequence(categories)

		assert output.shape == labels.shape

		sigmoided_output = torch.sigmoid(output.detach())
		accuracy_items = torch.round(sigmoided_output[mask]) == labels[mask]
		accuracy = torch.mean(accuracy_items.float())
		end_accuracy_items = torch.round(sigmoided_output[mask_exact]) == labels[mask_exact]
		end_accuracy = torch.mean(end_accuracy_items.float())

		all_accuracies.append(accuracy_items.cpu().numpy())
		all_end_accuracies.append(end_accuracy_items.cpu().numpy())

		# Data is (Sequence Index, Batch Index, Feature Index)
		for batch_index in range(output.shape[1]):
			flow_length = seq_lens[batch_index]
			flow_input = input_data[:flow_length,batch_index,:].detach().cpu().numpy()
			flow_output = output[:flow_length,batch_index,:].detach().cpu().numpy()
			assert (categories[0, batch_index,:] == categories[:flow_length, batch_index,:]).all()
			flow_category = int(categories[0, batch_index,:].squeeze().item())

			results_by_attack_numbers[flow_category].append(np.concatenate((flow_input, flow_output), axis=-1))

	file_name = opt.dataroot[:-7]+"_prediction_outcomes_{}_{}.pickle".format(opt.fold, opt.nFold)
	with open(file_name, "wb") as f:
		pickle.dump(results_by_attack_numbers, f)

	print("results_by_attack_numbers", [(index, len(item)) for index, item in enumerate(results_by_attack_numbers)])

	print("per-packet accuracy", np.mean(np.concatenate(all_accuracies)))
	print("per-flow end-accuracy", np.mean(np.concatenate(all_end_accuracies)))
	
def adv():

	# generate adversarial samples using Carlini  Wagner method
	n_fold = opt.nFold
	fold = opt.fold
	#lstm_module.train()
	
	#for param in lstm_module.features.parameters():
		#param.requires_grad = False

	#train_indices, _ = get_nth_split(dataset, n_fold, fold)
	
	#initialize sample
	indices = [i for i in range(len(y)) if y[i][0,0] == 1][:opt.batchSize]
	
	batch_x = [ torch.FloatTensor(x[i]) for i in indices ]

	lengths = torch.LongTensor([len(seq) for seq in batch_x])
	packed = torch.nn.utils.rnn.pack_sequence(batch_x, enforce_sorted=False)
	
	orig_batch = packed.data.clone()
	packed.data.requires_grad = True
	
	optimizer = optim.SGD([packed.data], lr=opt.lr)
	#optimizer = optim.SGD([sample], lr=opt.lr)
	
	# iterate until done
	
	for _ in range(200):

		print("iterating")
		# samples += len(input_data)
		optimizer.zero_grad()
		lstm_module.init_hidden(opt.batchSize)

		# actual_input = torch.FloatTensor(input_tensor[:,:,:-1]).to(device)

		# print("input_data.data.shape", input_data.data.shape)
		#s_squeezed = torch.nn.utils.rnn.pack_padded_sequence(torch.unsqueeze(sample, 1), [sample.shape[0]])
		output, seq_lens = lstm_module(packed)
		
		distance = ( (orig_batch - packed.data)**2 ).sum()
		#regularizer = .5*(torch.max(output[other_attacks]) - output[target_attack])
		#regularizer = .5*output[-1,0]
		regularizer = .5*torch.max(output, torch.FloatTensor([0])).sum()
		#if regularizer <= 0:
			#break
		criterion = distance + regularizer
		criterion.backward()
		
		print ('Distance: %f, regularizer: %f' % (distance, regularizer))
		
		# only consider lengths and iat
		packed.data.grad[:,:3] = 0
		packed.data.grad[:,5:] = 0
		optimizer.step()
		
		detached_batch = packed.data.detach()
		
		# Packet lengths cannot become smaller than original
		mask = detached_batch[:,3] < orig_batch[:,3]
		detached_batch[mask,3] = orig_batch[mask,3]
		
		# IAT cannot become smaller 0
		mask = detached_batch[:,4] * stds[4] + means[4] < 0
		detached_batch[mask,4] = float(-means[4]/stds[4])		

	seqs, lengths = torch.nn.utils.rnn.pad_packed_sequence(packed)
	adv_samples = [ seq[:length,:].detach().numpy()*stds + means for seq, length in zip(seqs, lengths) ]
	with open('adv_samples.pickle', 'wb') as outfile:	
		pickle.dump(adv_samples, outfile)
		
		
def eval_nn(seqs):

	lstm_module.eval()

	results = []
	
	for i in range(0,len(seqs),opt.batchSize):
		
		input_data = inputonly_collate(seqs[i:(i+opt.batchSize)])

		batch_size = input_data.sorted_indices.shape[0]
		lstm_module.init_hidden(opt.batchSize)

		output, seq_lens = lstm_module(input_data)
		#input_data, _ = torch.nn.utils.rnn.pad_packed_sequence(input_data)


		# Data is (Sequence Index, Batch Index, Feature Index)
		for batch_index in range(output.shape[1]):
			flow_length = seq_lens[batch_index]
			#flow_input = input_data[:flow_length,batch_index,:].detach().cpu().numpy()
			flow_output = output[:flow_length,batch_index,:].detach().cpu().numpy()

			results.append(flow_output)

	return results
	
def pred_plots():
	OUT_DIR='pred_plots'
	SAMPLES_PER_ATTACK = 10
	os.makedirs(OUT_DIR, exist_ok=True)
	lstm_module.eval()
	
	features = []
	# iat & length
	for feat_ind in [3, 4]:
		feat_min = min( (sample[0,feat_ind] for sample in x))
		feat_max = max( (sample[0,feat_ind] for sample in x))
		features.append((feat_ind,np.linspace(feat_min, feat_max, 100)))
	
	have_categories = collections.defaultdict(int)
	for sample_ind in range(len(x)):
		cat = categories[sample_ind][0,0]
		if have_categories[cat] == SAMPLES_PER_ATTACK:
			have_categories[cat] += 1

		flow = x[sample_ind]

		lstm_module.init_hidden(1)
		
		predictions = np.zeros((flow.shape[0],))
		mins = np.ones((len(features),flow.shape[0],))
		maxs = np.zeros((len(features),flow.shape[0],))
		
		for i in range(flow.shape[0]):
			
			lstm_module.forgetting = True
			
			for k, (feat_ind, values) in enumerate(features):
				packed_input = torch.nn.utils.rnn.pack_padded_sequence(torch.FloatTensor(flow[i,:][None,None,:]), [1])
				for j in range(values.size):
					packed_input.data[0,feat_ind] = values[j]
					output, _ = lstm_module(packed_input)
					sigmoided = torch.sigmoid(output[0,0,0])
					mins[k,i] = min(mins[k,i], sigmoided)
					maxs[k,i] = max(maxs[k,i], sigmoided)
				
			lstm_module.forgetting = False
			packed_input = torch.nn.utils.rnn.pack_padded_sequence(torch.FloatTensor(flow[i,:][None,None,:]), [1])
			output, _ = lstm_module(packed_input)
			predictions[i] = torch.sigmoid(output[0,0,0])

		np.save('%s/%d_%d.npy' % (OUT_DIR, cat, sample_ind), np.vstack((predictions,mins,maxs)))
	

def pdp():
		
	PDP_DIR = 'pdps'
	# TODO: consider fold
	for feat_name, feat_ind in [('srcPort', 0), ('dstPort', 1)]:
		feat_min = min( (sample[0,feat_ind] for sample in x))
		feat_max = max( (sample[0,feat_ind] for sample in x))
		
		values = np.linspace(feat_min, feat_max, 100)
		
		subset = [ torch.FloatTensor(sample) for sample in x[:opt.batchSize] ]
		
		pdp = np.zeros([values.size])
		
		for i in range(values.size):
			for sample in subset:
				for j in range(sample.shape[0]):
					sample[j,feat_ind] = values[i]
			outputs = eval_nn(subset)
			# TODO: avg. or end output?
			pdp[i] = sum((torch.sigmoid(output[-1]) for output in outputs)) / len(subset)
			
		rescaled = values * stds[feat_ind] + means[feat_ind]
		os.makedirs(PDP_DIR, exist_ok=True)
		np.save('%s/%s.npy' % (PDP_DIR, feat_name), np.vstack((rescaled,pdp)))

if __name__=="__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument('--dataroot', required=True, help='path to dataset')
	parser.add_argument('--normalizationData', default="", type=str, help='normalization data to use')
	parser.add_argument('--fold', type=int, default=0, help='fold to use')
	parser.add_argument('--nFold', type=int, default=10, help='total number of folds')
	parser.add_argument('--batchSize', type=int, default=128, help='input batch size')
	parser.add_argument('--net', default='', help="path to net (to continue training)")
	parser.add_argument('--function', default='train', help='the function that is going to be called')
	parser.add_argument('--manualSeed', default=0, type=int, help='manual seed')
	parser.add_argument('--maxLength', type=int, default=1000, help='max length')
	parser.add_argument('--maxSize', type=int, default=sys.maxsize, help='limit of samples to consider')
	parser.add_argument("--categoriesMapping", type=str, default="categories_mapping.json", help="mapping of attack categories; see parse.py")
	parser.add_argument('--lr', type=float, default=10**(-2), help='learning rate')

	opt = parser.parse_args()
	print(opt)
	SEED = opt.manualSeed
	random.seed(SEED)
	np.random.seed(SEED)
	torch.manual_seed(SEED)

	with open (opt.dataroot, "rb") as f:
		all_data = pickle.load(f)

	all_data = [item[:opt.maxLength,:] for item in all_data]
	random.shuffle(all_data)
	all_data = all_data[:opt.maxSize]
	# print("lens", [len(item) for item in all_data])
	x = [item[:, :-2] for item in all_data]
	y = [item[:, -1:] for item in all_data]
	categories = [item[:, -2:-1] for item in all_data]

	if opt.normalizationData == "":
		file_name = opt.dataroot[:-7]+"_normalization_data.pickle"
		catted_x = np.concatenate(x, axis=0)
		means = np.mean(catted_x, axis=0)
		stds = np.std(catted_x, axis=0)
		stds[stds==0.0] = 1.0

		with open(file_name, "wb") as f:
			f.write(pickle.dumps((means, stds)))
	else:
		file_name = opt.normalizationData
		with open(file_name, "rb") as f:
			means, stds = pickle.load(f)
	assert means.shape[0] == x[0].shape[-1], "means.shape: {}, x.shape: {}".format(means.shape, x[0].shape)
	assert stds.shape[0] == x[0].shape[-1], "stds.shape: {}, x.shape: {}".format(stds.shape, x[0].shape)
	assert not (stds==0).any(), "stds: {}".format(stds)
	x = [(item-means)/stds for item in x]

	cuda_available = torch.cuda.is_available()
	device = torch.device("cuda:0" if cuda_available else "cpu")

	dataset = OurDataset(x, y, categories)

	batchSize = 1 if opt.function == 'pred_plots' else opt.batchSize # fixme
	lstm_module = OurLSTMModule(x[0].shape[-1], y[0].shape[-1], HIDDEN_SIZE, N_LAYERS, batchSize, device).to(device)

	if opt.net != '':
		print("Loading", opt.net)
		lstm_module.load_state_dict(torch.load(opt.net, map_location=device))

	globals()[opt.function]()
