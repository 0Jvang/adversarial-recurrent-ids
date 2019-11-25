#!/usr/bin/env python3

import sys
import os
import pickle
import json
import math
import argparse
import random
import time
from functools import reduce
from tqdm import tqdm

import collections
import itertools
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from tensorboardX import SummaryWriter
import matplotlib.pyplot as plt
from sklearn.metrics import accuracy_score,recall_score, precision_score, f1_score, balanced_accuracy_score

ADVERSARIAL_THRESH = 50

def output_scores(y_true, y_pred):
	accuracy = accuracy_score(y_true, y_pred)
	precision = precision_score(y_true, y_pred)
	recall = recall_score(y_true, y_pred)
	f1 = f1_score(y_true, y_pred)
	youden = balanced_accuracy_score(y_true, y_pred, adjusted=True)
	metrics = ['Accuracy', 'Precision', 'Recall', 'F1', 'Youden']
	print (('{:>11}'*len(metrics)).format(*metrics))
	print ((' {:.8f}'*len(metrics)).format(accuracy, precision, recall, f1, youden))

def numpy_sigmoid(x):
	return 1/(1+np.exp(-x))

class OurDataset(Dataset):
	def __init__(self, data, labels, categories):
		self.data = data
		self.labels = labels
		self.categories = categories
		assert(len(self.data) == len(self.labels) == len(self.categories))

	def __getitem__(self, index):
		data, labels, categories = torch.FloatTensor(self.data[index]), torch.FloatTensor(self.labels[index]), torch.FloatTensor(self.categories[index])
		return data, labels, categories

	def __len__(self):
		return len(self.data)

class AdvDataset(Dataset):
	def __init__(self, base_dataset):
		self.base_dataset = base_dataset
		self.adv_flows = []
		self.categories = []

	def __getitem__(self, index):
		base_len = len(self.base_dataset)
		if index < base_len:
			return self.base_dataset.__getitem__(index)
		else:
			flow = self.adv_flows[index - base_len]
			category = self.categories[index - base_len] + ADVERSARIAL_THRESH
			data = torch.FloatTensor(flow)
			labels = torch.ones((flow.shape[0], 1))
			categories = torch.ones((flow.shape[0], 1)) * category
			return data, labels, categories

	def __len__(self):
		return len(self.base_dataset) + len(self.adv_flows)

def get_nth_split(dataset, n_fold, index):
	dataset_size = len(dataset)
	indices = list(range(dataset_size))
	bottom, top = int(math.floor(float(dataset_size)*index/n_fold)), int(math.floor(float(dataset_size)*(index+1)/n_fold))
	train_indices, test_indices = indices[0:bottom]+indices[top:], indices[bottom:top]
	return train_indices[:opt.maxSize], test_indices[:opt.maxSize]

# Shouldn't be used anymore
# def custom_dropout(input_tensor, dim, p):
# 	relevant_dim_len = input_tensor.shape[dim]
# 	d = torch.distributions.bernoulli.Bernoulli(torch.tensor([p]*relevant_dim_len))
# 	s = d.sample().to(device)
# 	# FIXME: Hack; should be changed by changing the probability
# 	inverse_s = 1.0-s

# 	for i in range(dim):
# 		inverse_s = inverse_s.unsqueeze(0)
# 	for i in range(dim+1, len(input_tensor.shape)):
# 		inverse_s = inverse_s.unsqueeze(i)
# 	assert len(inverse_s.shape) == len(input_tensor.shape)

# 	repetition_sequence = [1 if dim==orig_dim else input_tensor.shape[orig_dim] for orig_dim in range(len(input_tensor.shape))]
# 	inverse_s = inverse_s.repeat(*repetition_sequence)
# 	assert list(inverse_s.shape) == list(input_tensor.shape)

# 	input_tensor = torch.mul(input_tensor, inverse_s)
# 	input_tensor = torch.cat((input_tensor, inverse_s), dim=dim)
# 	return input_tensor

class OurLSTMModule(nn.Module):
	def __init__(self, num_inputs, num_outputs, hidden_size, n_layers, batch_size, device, forgetting=False):
		super(OurLSTMModule, self).__init__()
		# if opt.averageFeaturesToPruneDuringTraining!=-1:
		# 	self.feature_dropout_probability = opt.averageFeaturesToPruneDuringTraining/num_inputs
		self.hidden_size = hidden_size
		self.n_layers = n_layers
		self.num_inputs = num_inputs if opt.averageFeaturesToPruneDuringTraining==-1 else num_inputs*2
		self.num_outputs = num_outputs
		self.batch_size = batch_size
		self.device = device
		self.lstm = nn.LSTM(input_size=num_inputs if opt.averageFeaturesToPruneDuringTraining==-1 else num_inputs*2, hidden_size=hidden_size, num_layers=n_layers)
		self.hidden = None
		self.h2o = nn.Linear(hidden_size, num_outputs)
		self.forgetting = forgetting

	def init_hidden(self, batch_size):
		self.hidden = (torch.zeros(self.n_layers, batch_size, self.hidden_size).to(self.device),
		torch.zeros(self.n_layers, batch_size, self.hidden_size).to(self.device))

	def forward(self, batch):
		assert not opt.function=="test" or not self.training
		# if opt.averageFeaturesToPruneDuringTraining!=-1:
		# 	p = self.feature_dropout_probability if self.training else 0.0
		# 	batch.data.data = custom_dropout(batch.data.data, 1, p)
		lstm_out, new_hidden = self.lstm(batch, self.hidden)
		if not self.forgetting:
			self.hidden = new_hidden
		lstm_out, seq_lens = torch.nn.utils.rnn.pad_packed_sequence(lstm_out)
		output = self.h2o(lstm_out)
		return output, seq_lens

def get_one_hot_vector(class_indices, num_classes, batch_size):
	y_onehot = torch.FloatTensor(batch_size, num_classes)
	y_onehot.zero_()
	return y_onehot.scatter_(1, class_indices.unsqueeze(1), 1)

def custom_collate(seqs, things=(True, True, True)):
	seqs, labels, categories = zip(*seqs)
	assert len(seqs) == len(labels) == len(categories)
	return [collate_things(item, index==0) for index, (item, thing) in enumerate(zip((seqs, labels, categories), things)) if thing]

def unpad_padded_sequences(sequences, lengths):
	unbound = [seq[:len] for seq, len in zip(torch.unbind(sequences, 1), lengths)]
	return unbound

def bernoullize_seq(seq, p):
	relevant_dim_len = seq.shape[1]
	d = torch.distributions.bernoulli.Bernoulli(torch.tensor([p]*relevant_dim_len))
	s = d.sample()
	# FIXME: Hack; should be changed by changing the probability
	inverse_s = 1.0-s

	inverse_s = inverse_s.unsqueeze(0)
	inverse_s = inverse_s.repeat(seq.shape[0], 1)

	seq = torch.mul(seq, inverse_s)
	seq = torch.cat((seq, inverse_s), dim=1)

	return seq

def collate_things(seqs, is_seqs):
	# import pdb; pdb.set_trace()
	if is_seqs and opt.averageFeaturesToPruneDuringTraining!=-1:
		assert not opt.function=="dropout_feature_importance" or not lstm_module.training
		assert not opt.function=="test" or not lstm_module.training
		feature_dropout_probability = opt.averageFeaturesToPruneDuringTraining/seqs[0].shape[1]
		p = feature_dropout_probability if lstm_module.training else 0.0
		# XXX Next line must be commented out!!!
		# p = feature_dropout_probability
		seqs = tuple(bernoullize_seq(item, p) for item in seqs)

	seq_lengths = torch.LongTensor([len(seq) for seq in seqs]).to(device)
	seq_tensor = torch.nn.utils.rnn.pad_sequence(seqs).to(device)

	packed_input = torch.nn.utils.rnn.pack_padded_sequence(seq_tensor, seq_lengths, enforce_sorted=False)
	return packed_input

def adv_filename():
	return os.path.splitext(opt.dataroot)[0] + '.adv.pickle'

def train():

	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.train()

	train_indices, _ = get_nth_split(dataset, n_fold, fold)
	train_data = torch.utils.data.Subset(dataset, train_indices)
	if opt.advTraining:
		train_data = AdvDataset(train_data)
		adv_generator = adv_internal(in_training=True)
	train_loader = torch.utils.data.DataLoader(train_data, batch_size=opt.batchSize, shuffle=True, collate_fn=custom_collate)

	optimizer = optim.SGD(lstm_module.parameters(), lr=opt.lr)
	criterion = nn.BCEWithLogitsLoss(reduction="mean")

	writer = SummaryWriter()

	samples = 0
	for i in range(1, sys.maxsize):
		if opt.advTraining:
			train_data.adv_flows, train_data.categories, av_distance = next(adv_generator)
			writer.add_scalar('adv_avdistance', av_distance, i)

		for input_data, labels, flow_categories in train_loader:
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

			mask = (index_tensor <= selection_tensor).byte().to(device)
			mask_exact = (index_tensor == selection_tensor).byte().to(device)
			# torch.set_printoptions(profile="full")
			# print("mask", mask.squeeze())
			labels, _ = torch.nn.utils.rnn.pad_packed_sequence(labels)
			flow_categories, _ = torch.nn.utils.rnn.pad_packed_sequence(flow_categories)

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

			adv_mask = flow_categories >= ADVERSARIAL_THRESH
			if adv_mask.sum() > 0:
				mask &= adv_mask
				mask_exact &= adv_mask

				accuracy = torch.mean((torch.round(sigmoided_output[mask]) == labels[mask]).float())
				writer.add_scalar("adv_accuracy", accuracy, samples)
				end_accuracy = torch.mean((torch.round(sigmoided_output[mask_exact]) == labels[mask_exact]).float())
				writer.add_scalar("adv_end_accuracy", end_accuracy, samples)
				writer.add_scalar("adv_confidence", torch.mean(confidences[mask]), samples)
				writer.add_scalar("adv_end_confidence", torch.mean(confidences[mask_exact]), samples)


		# Save after every epoch
		if i % 1 == 0:
			torch.save(lstm_module.state_dict(), '%s/lstm_module_%d.pth' % (writer.log_dir, i))
			if opt.advTraining:
				with open(adv_filename(), 'wb') as f:
					pickle.dump(train_data.adv_flows, f)

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

	attack_numbers = mapping.values()
	reverse_mapping = {v: k for k, v in mapping.items()}

	results_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	label_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	sample_indices_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	for input_data, labels, categories in tqdm(test_loader):

		batch_size = input_data.sorted_indices.shape[0]
		assert batch_size <= opt.batchSize, "batch_size: {}, opt.batchSize: {}".format(batch_size, opt.batchSize)
		lstm_module.init_hidden(batch_size)

		output, seq_lens = lstm_module(input_data)

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
			flow_label = int(labels[0, batch_index,:].squeeze().item())

			results_by_attack_number[flow_category].append(np.concatenate((flow_input, flow_output), axis=-1))
			label_by_attack_number[flow_category] = flow_label
			sample_indices_by_attack_number[flow_category].append(test_indices[samples])

			samples += 1

	file_name = opt.dataroot[:-7]+"_prediction_outcomes_{}_{}.pickle".format(opt.fold, opt.nFold)

	all_results_concatenated = [(np.concatenate(item, axis=0) if len(item) > 0 else []) for item in results_by_attack_number]
	attack_by_attack_number = [[label_by_attack_number[index]]*len(item) for index, item in enumerate(all_results_concatenated)]

	all_predictions = (np.round(numpy_sigmoid(np.concatenate([item for item in all_results_concatenated if item!=[]], axis=0))).astype(int))[:,-1]
	all_labels = [subitem for item in attack_by_attack_number for subitem in item]

	print("Packet metrics:")
	output_scores(all_labels, all_predictions)

	all_results_concatenated = [(np.concatenate([subitem[-1:,:] for subitem in item], axis=0) if len(item) > 0 else []) for item in results_by_attack_number]
	attack_by_attack_number = [[label_by_attack_number[index]]*len(item) for index, item in enumerate(all_results_concatenated)]

	all_predictions = (np.round(numpy_sigmoid(np.concatenate([item for item in all_results_concatenated if item!=[]], axis=0))).astype(int))[:,-1]
	all_labels = [subitem for item in attack_by_attack_number for subitem in item]

	print("Flow metrics:")
	output_scores(all_labels, all_predictions)

	with open(file_name, "wb") as f:
		pickle.dump({"results_by_attack_number": results_by_attack_number, "sample_indices_by_attack_number": sample_indices_by_attack_number}, f)

# This function takes a model that was trained with feature dropout and can compute features that are contain overlapping information (currently it looks at all combinations of features).
def dropout_feature_correlation():

	baseline_accuracy, accuracy_by_feature = dropout_feature_importance()

	# These features are constant throughout a flow
	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	assert opt.averageFeaturesToPruneDuringTraining!=-1

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	test_data = torch.utils.data.Subset(dataset, test_indices)
	test_x = np.concatenate([item[0][:,:] for item in test_data], axis=0).transpose(1,0)
	test_loader = torch.utils.data.DataLoader(test_data, batch_size=opt.batchSize, shuffle=False, collate_fn=custom_collate)

	attack_numbers = mapping.values()

	dropout_results_by_attack_number = [{(i,j):[] for i in range(test_x.shape[0]) for j in range(i+1, test_x.shape[0])} for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	for input_data, labels, categories in test_loader:

		batch_size = input_data.sorted_indices.shape[0]
		assert batch_size <= opt.batchSize, "batch_size: {}, opt.batchSize: {}".format(batch_size, opt.batchSize)

		# input_data, _ = torch.nn.utils.rnn.pad_packed_sequence(input_data)
		labels_padded, _ = torch.nn.utils.rnn.pad_packed_sequence(labels)
		categories_padded, _ = torch.nn.utils.rnn.pad_packed_sequence(categories)

		for feat1 in range(test_x.shape[0]):
			for feat2 in range(feat1+1, test_x.shape[0]):
				assert feat1!=feat2
				# Now draw individual feature values at random and query the nn for modified flows
				lstm_module.init_hidden(batch_size)

				input_data_cloned = torch.nn.utils.rnn.PackedSequence(input_data.data.detach().clone(), input_data.batch_sizes, input_data.sorted_indices, input_data.unsorted_indices)
				input_data_cloned.data.data[:,feat1] = 0.0
				input_data_cloned.data.data[:,feat2] = 0.0
				offset = input_data_cloned.data.data.shape[1]/2
				assert offset == int(offset), f"{offset}, {int(offset)}"
				input_data_cloned.data.data[:,int(offset)+feat1] = 0.0
				input_data_cloned.data.data[:,int(offset)+feat2] = 0.0

				output, seq_lens = lstm_module(input_data_cloned)
				assert output.shape == labels_padded.shape

				sigmoided_output = torch.sigmoid(output.detach())

				# Data is (Sequence Index, Batch Index, Feature Index)
				for batch_index in range(output.shape[1]):
					flow_length = seq_lens[batch_index]
					flow_output = (torch.round(sigmoided_output[:flow_length,batch_index,:]) == labels_padded[:flow_length,batch_index,:]).detach().cpu().numpy()
					assert (categories_padded[0, batch_index,:] == categories_padded[:flow_length, batch_index,:]).all()
					flow_category = int(categories_padded[0, batch_index,:].squeeze().item())
					dropout_results_by_attack_number[flow_category][(feat1, feat2)].append(flow_output)

	with open("features.json", "r") as f:
		feature_array = json.load(f)

	# TODO: Rename correlations to something like "separate information"
	correlations = {}
	joint_accuracy = {}
	print("all scores:")
	for feat1 in range(test_x.shape[0]):
		for feat2 in range(feat1+1, test_x.shape[0]):
			accuracy_for_feature_1 = accuracy_by_feature[feat1]
			accuracy_for_feature_2 = accuracy_by_feature[feat2]
			accuracy_for_feature_combination = np.mean(np.concatenate([feature for attack_type in dropout_results_by_attack_number for feature in attack_type[(feat1,feat2)]]))
			joint_accuracy[(feat1, feat2)] = accuracy_for_feature_combination
			numerator = max(float(baseline_accuracy-accuracy_for_feature_combination), 0)
			denominator = max(float(baseline_accuracy-accuracy_for_feature_1), 0) + max(float(baseline_accuracy-accuracy_for_feature_2), 0)
			try:
				overall_score = numerator/denominator
			# accuracy doesn't fall if either feat1 or feat2 are omitted
			except ZeroDivisionError:
				# If the accuracy falls if for both features are omitted, they are kind of infinitely correlated?
				if numerator > 0:
					overall_score = float("inf")
				# Otherwise, accuracy doesn't fall if both feat1 and feat2 are omitted and also not if either one is omitted, I'd say they are not correlated at all. It could be set to nan or 0 or 1 I guess. This happens if the features contain no information whatsoever...
				else:
					overall_score = float("nan")
			correlations[(feat1, feat2)] = overall_score
			print("feat1", feat1, feature_array[feat1], feat2, "feat2", feature_array[feat2], "acc_feat1", baseline_accuracy-accuracy_by_feature[feat1], "acc_feat2", baseline_accuracy-accuracy_by_feature[feat2], "joint_acc", joint_accuracy[(feat1, feat2)], "score", overall_score)

	N_MOST_CORRELATED_TO_SHOW = len(list(correlations.items()))
	sorted_correlations = list(sorted([item for item in list(correlations.items()) if not math.isnan(item[1])], key=lambda item: item[1], reverse=True))[:N_MOST_CORRELATED_TO_SHOW]
	print("highest", N_MOST_CORRELATED_TO_SHOW, "scores:")
	for (feat1, feat2), score in sorted_correlations:
		print("feat1", feat1, feature_array[feat1], feat2, "feat2", feature_array[feat2], "acc_feat1", baseline_accuracy-accuracy_by_feature[feat1], "acc_feat2", baseline_accuracy-accuracy_by_feature[feat2], "joint_acc", joint_accuracy[(feat1, feat2)], "score", score)

# This function takes a model that was trained with feature dropout and can compute feature importance using that model.
def dropout_feature_importance():

	# These features are constant throughout a flow
	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	assert opt.averageFeaturesToPruneDuringTraining!=-1

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	test_data = torch.utils.data.Subset(dataset, test_indices)
	test_x = np.concatenate([item[0][:,:] for item in test_data], axis=0).transpose(1,0)
	test_loader = torch.utils.data.DataLoader(test_data, batch_size=opt.batchSize, shuffle=False, collate_fn=custom_collate)

	attack_numbers = mapping.values()

	results_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	dropout_results_by_attack_number = [[list() for _ in range(test_x.shape[0])] for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	for input_data, labels, categories in test_loader:

		# Query neural network for unmodified flows
		batch_size = input_data.sorted_indices.shape[0]
		assert batch_size <= opt.batchSize, "batch_size: {}, opt.batchSize: {}".format(batch_size, opt.batchSize)
		lstm_module.init_hidden(batch_size)

		output, seq_lens = lstm_module(input_data)

		index_tensor = torch.arange(0, output.shape[0], dtype=torch.int64).unsqueeze(1).unsqueeze(2).repeat(1, output.shape[1], output.shape[2])
		selection_tensor = seq_lens.unsqueeze(0).unsqueeze(2).repeat(index_tensor.shape[0], 1, index_tensor.shape[2])-1
		mask = (index_tensor <= selection_tensor).byte().to(device)
		# mask_exact = (index_tensor == selection_tensor).byte().to(device)

		# input_data, _ = torch.nn.utils.rnn.pad_packed_sequence(input_data)
		labels_padded, _ = torch.nn.utils.rnn.pad_packed_sequence(labels)
		categories_padded, _ = torch.nn.utils.rnn.pad_packed_sequence(categories)

		assert output.shape == labels_padded.shape

		sigmoided_output = torch.sigmoid(output.detach())

		# Data is (Sequence Index, Batch Index, Feature Index)
		for batch_index in range(output.shape[1]):
			flow_length = seq_lens[batch_index]
			flow_output = (torch.round(sigmoided_output[:flow_length,batch_index,:]) == labels_padded[:flow_length,batch_index,:]).detach().cpu().numpy()
			assert (categories_padded[0, batch_index,:] == categories_padded[:flow_length, batch_index,:]).all()
			flow_category = int(categories_padded[0, batch_index,:].squeeze().item())

			results_by_attack_number[flow_category].append(flow_output)

		for feature_index in range(test_x.shape[0]):
			# Now draw individual feature values at random and query the nn for modified flows
			lstm_module.init_hidden(batch_size)

			input_data_cloned = torch.nn.utils.rnn.PackedSequence(input_data.data.detach().clone(), input_data.batch_sizes, input_data.sorted_indices, input_data.unsorted_indices)
			input_data_cloned.data.data[:,feature_index] = 0.0
			offset = input_data_cloned.data.data.shape[1]/2
			assert offset == int(offset), f"{offset}, {int(offset)}"
			input_data_cloned.data.data[:,int(offset)+feature_index] = 0.0

			output, seq_lens = lstm_module(input_data_cloned)

			sigmoided_output = torch.sigmoid(output.detach())

			# Data is (Sequence Index, Batch Index, Feature Index)
			for batch_index in range(output.shape[1]):
				flow_length = seq_lens[batch_index]

				flow_output = (torch.round(sigmoided_output[:flow_length,batch_index,:]) == labels_padded[:flow_length,batch_index,:]).detach().cpu().numpy()

				assert (categories_padded[0, batch_index,:] == categories_padded[:flow_length, batch_index,:]).all()

				flow_category = int(categories_padded[0, batch_index,:].squeeze().item())

				dropout_results_by_attack_number[flow_category][feature_index].append(flow_output)

	accuracy = np.mean(np.concatenate([subitem for item in results_by_attack_number for subitem in item], axis=0))
	accuracy_for_features = []
	with open("features.json", "r") as f:
		feature_array = json.load(f)
	print("accuracy", accuracy)
	for feature_index in range(test_x.shape[0]):
		accuracy_for_feature = np.mean(np.concatenate([feature for attack_type in dropout_results_by_attack_number for feature in attack_type[feature_index]]))
		accuracy_for_features.append(accuracy_for_feature)
		print("accuracy_for_feature", feature_index, feature_array[feature_index], accuracy-accuracy_for_feature)
	return (accuracy, accuracy_for_features)

def get_feature_importance_distribution(test_data):
	distribution = np.concatenate([item[0][:,:] for item in test_data], axis=0).transpose(1,0)
	if opt.adjustFeatImpDistribution:
		numerical_features = [0,1,3]
		# retain original distribution of iat, as iat varies over many magnitudes and we're mainly
		# interested in feature importance of practical (=low) iats
		retain_features = [2]
		for feature in distribution.shape[0]:
			if feature in numerical_features:
				distribution[feature,:] = np.linspace(np.min(distribution[feature,:]), np.max(distribution[feature,:]), distribution.shape[1])
			elif feature not in retain_features:
				unique = np.unique(distribution[feature,:])
				distribution[feature,:] = np.random.choice(unique, size=distribution.shape[1])
	return distribution
	
# Right now this function replaces all values of one feature by random values sampled from the distribution of all features and looks how the accuracy changes.
def feature_importance():

	# Number of bins for probability density functions
	PDF_FEATURE_BINS = 50
	PDF_CONFIDENCE_BINS = 2

	# These features are constant throughout a flow
	constant_features = [0,1,2]
	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	test_data = torch.utils.data.Subset(dataset, test_indices)
	distribution = get_feature_importance_distribution(test_data)
	test_loader = torch.utils.data.DataLoader(test_data, batch_size=opt.batchSize, shuffle=False, collate_fn=custom_collate)

	attack_numbers = mapping.values()

	results_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	randomized_results_by_attack_number = [[list() for _ in range(distribution.shape[0])] for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	if opt.mutinfo:
		minmax = list(zip(np.min(distribution, axis=1), np.max(distribution, axis=1)))

		# Joint pdf for feature values in individual time steps and confidence in the same time step
		per_packet_pdf = np.zeros([opt.maxLength,distribution.shape[0],PDF_FEATURE_BINS,PDF_CONFIDENCE_BINS])
		# Joint pdf for constant feature values and end confidence
		per_flow_pdf = np.zeros([max(constant_features)+1,PDF_FEATURE_BINS,PDF_CONFIDENCE_BINS])

	for input_data, labels, categories in test_loader:

		# Query neural network for unmodified flows
		batch_size = input_data.sorted_indices.shape[0]
		assert batch_size <= opt.batchSize, "batch_size: {}, opt.batchSize: {}".format(batch_size, opt.batchSize)
		lstm_module.init_hidden(batch_size)

		output, seq_lens = lstm_module(input_data)

		index_tensor = torch.arange(0, output.shape[0], dtype=torch.int64).unsqueeze(1).unsqueeze(2).repeat(1, output.shape[1], output.shape[2])
		selection_tensor = seq_lens.unsqueeze(0).unsqueeze(2).repeat(index_tensor.shape[0], 1, index_tensor.shape[2])-1
		mask = (index_tensor <= selection_tensor).byte().to(device)
		# mask_exact = (index_tensor == selection_tensor).byte().to(device)

		# input_data, _ = torch.nn.utils.rnn.pad_packed_sequence(input_data)
		labels_padded, _ = torch.nn.utils.rnn.pad_packed_sequence(labels)
		categories_padded, _ = torch.nn.utils.rnn.pad_packed_sequence(categories)

		assert output.shape == labels_padded.shape

		sigmoided_output = torch.sigmoid(output.detach())

		# Data is (Sequence Index, Batch Index, Feature Index)
		for batch_index in range(output.shape[1]):
			flow_length = seq_lens[batch_index]
			flow_output = (torch.round(sigmoided_output[:flow_length,batch_index,:]) == labels_padded[:flow_length,batch_index,:]).detach().cpu().numpy()
			assert (categories_padded[0, batch_index,:] == categories_padded[:flow_length, batch_index,:]).all()
			flow_category = int(categories_padded[0, batch_index,:].squeeze().item())

			results_by_attack_number[flow_category].append(flow_output)

		for feature_index in range(distribution.shape[0]):
			# Now draw individual feature values at random and query the nn for modified flows
			lstm_module.init_hidden(batch_size)

			input_data_cloned = torch.nn.utils.rnn.PackedSequence(input_data.data.detach().clone(), input_data.batch_sizes, input_data.sorted_indices, input_data.unsorted_indices)
			if feature_index in constant_features:
				input_data_padded, input_data_lens = torch.nn.utils.rnn.pad_packed_sequence(input_data_cloned)
				input_data_padded[:,:,feature_index] = torch.FloatTensor(np.random.choice(distribution[feature_index], size=(1,input_data_padded.shape[1]))).to(device)
				input_data_cloned = torch.nn.utils.rnn.pack_padded_sequence(input_data_padded, input_data_lens, enforce_sorted=False)
			else:
				input_data_cloned.data.data[:,feature_index] = torch.FloatTensor(np.random.choice(distribution[feature_index], size=(input_data_cloned.data.data.shape[0]))).to(device)
			output, seq_lens = lstm_module(input_data_cloned)

			sigmoided_output = torch.sigmoid(output.detach())

			# Data is (Sequence Index, Batch Index, Feature Index)
			for batch_index in range(output.shape[1]):
				flow_length = seq_lens[batch_index]

				if opt.mutinfo:
					if feature_index in constant_features:
						bin1 = int(torch.round((PDF_FEATURE_BINS-1) * (input_data_padded[0,batch_index,feature_index] - minmax[feature_index][0]) / (minmax[feature_index][1]-minmax[feature_index][0])))
						bin2 = int(torch.round((PDF_CONFIDENCE_BINS-1) * sigmoided_output[flow_length-1,batch_index,0]))
						per_flow_pdf[feature_index,bin1,bin2] += 1

					bin1 = (torch.round((PDF_FEATURE_BINS-1) * (input_data_padded[:flow_length,batch_index,feature_index] - minmax[feature_index][0]) / (minmax[feature_index][1]-minmax[feature_index][0]))).cpu().numpy().astype(int)
					bin2 = (torch.round((PDF_CONFIDENCE_BINS-1) * sigmoided_output[:flow_length,batch_index,0])).cpu().numpy().astype(int)
					per_packet_pdf[np.arange(bin1.size),feature_index,bin1,bin2] += 1

				flow_output = (torch.round(sigmoided_output[:flow_length,batch_index,:]) == labels_padded[:flow_length,batch_index,:]).detach().cpu().numpy()
				assert (categories_padded[0, batch_index,:] == categories_padded[:flow_length, batch_index,:]).all()
				flow_category = int(categories_padded[0, batch_index,:].squeeze().item())

				randomized_results_by_attack_number[flow_category][feature_index].append(flow_output)

	accuracy = np.mean(np.concatenate([subitem for item in results_by_attack_number for subitem in item], axis=0))
	with open("features.json", "r") as f:
		feature_array = json.load(f)
	print("accuracy", accuracy)
	mutinfos = []
	for feature_index in range(distribution.shape[0]):
		accuracy_for_feature = np.mean(np.concatenate([feature for attack_type in randomized_results_by_attack_number for feature in attack_type[feature_index]]))
		print("accuracy_for_feature", feature_index, feature_array[feature_index], accuracy-accuracy_for_feature)
		if opt.mutinfo:
			if feature_index in constant_features:
				print("mutual information for pf feature", feature_index, feature_array[feature_index], compute_mutinfo(per_flow_pdf[feature_index,:,:]))
			print("mutual information for pp feature", feature_index, feature_array[feature_index], compute_mutinfo(np.sum(per_packet_pdf[:,feature_index,:,:],axis=0)))
			mutinfos.append([
				compute_mutinfo(np.sum(per_packet_pdf[:,feature_index,:,:], axis=0)),
				[ compute_mutinfo(per_packet_pdf[timestep,feature_index,:,:]) for timestep in range(opt.maxLength) ]
			])
			if feature_index in constant_features:
				mutinfos[-1].append(compute_mutinfo(per_flow_pdf[feature_index,:,:]))

	if opt.mutinfo:
		with open(opt.dataroot[:-7] + '_mutinfos.pickle', 'wb') as f:
			pickle.dump(mutinfos, f)

def compute_mutinfo(joint_pdf):
	joint_pdf = joint_pdf / np.sum(joint_pdf)
	marg_1 = np.sum(joint_pdf, axis=1)
	marg_1 /= np.sum(marg_1)
	marg_2 = np.sum(joint_pdf, axis=0)
	marg_2 /= np.sum(marg_2)

	# For 0 values, joint_pdf is 0 as well. Prevent NaNs in this case
	marg_1[marg_1==0] = 1
	marg_2[marg_2==0] = 1
	nonzero_joint_pdf = joint_pdf.copy()
	nonzero_joint_pdf[joint_pdf==0] = 1

	return np.sum(joint_pdf * np.log2( nonzero_joint_pdf / (marg_1[:,None] * marg_2[None,:])))

def mutinfo_feat_imp():
	constant_features = [0,1,2]
	SAMPLE_COUNT = 100
	PDF_FEATURE_BINS = 50
	PDF_CONFIDENCE_BINS = 2
	
	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	test_data = torch.utils.data.Subset(dataset, test_indices)
	distribution = get_feature_importance_distribution(test_data)

	minmax = list(zip(np.min(distribution, axis=1), np.max(distribution, axis=1)))
	features = [ feat_ind for feat_ind in range(distribution.shape[0]) if feat_ind not in constant_features and minmax[feat_ind][0] != minmax[feat_ind][1] ]

	mutinfos = np.zeros([opt.maxLength, distribution.shape[0]])
	flow_lengths = np.zeros(opt.maxLength, dtype=int)

	start_iterating = time.time()
	
	for real_ind, sample_ind in zip(test_indices, range(len(test_data))):
		print (sample_ind)
		flow, _, flow_categories = test_data[sample_ind]
		cat = int(flow_categories[0,0])
		flow_lengths[:flow.shape[0]] += 1
		
		# process constant features
		lstm_module.init_hidden(len(constant_features)*SAMPLE_COUNT)
		input_data = torch.FloatTensor(flow[:,None,:]).repeat(1,len(constant_features)*SAMPLE_COUNT,1)
		
		for k, feat_ind in enumerate(constant_features):
			input_data[:,k*SAMPLE_COUNT:(k+1)*SAMPLE_COUNT,feat_ind] = torch.FloatTensor(np.random.choice(distribution[feat_ind,:], size=(input_data.shape[0],SAMPLE_COUNT)))

		packed_input = torch.nn.utils.rnn.pack_padded_sequence(input_data, [input_data.shape[0]] *input_data.shape[1]).to(device)
		
		for i in range(flow.shape[0]):

			output, _ = lstm_module(packed_input)
			sigmoided = torch.sigmoid(output[0,:,0]).detach().cpu()
			
			for k,feat_ind in enumerate(features):
				bin1 = (torch.round((PDF_FEATURE_BINS-1) * (input_data[i,:,feat_ind] - minmax[feat_ind][0]) / (minmax[feat_ind][1]-minmax[feat_ind][0]))).cpu().numpy().astype(int)
				bin2 = (torch.round((PDF_CONFIDENCE_BINS-1) * sigmoided)).cpu().numpy().astype(int)
				c = Counter(zip(bin1,bin2))
				bin1, bin2 = zip(*c.keys())
				joint_pdf = np.zeros([PDF_FEATURE_BINS,PDF_CONFIDENCE_BINS])
				joint_pdf[bin1,bin2] = list(c.values())
				mutinfos[i,feat_ind] += compute_mutinfo(joint_pdf)


		# process variables features
		lstm_module.init_hidden(1)

		for i in range(flow.shape[0]):
			lstm_module.forgetting = True

			input_data = torch.FloatTensor(flow[i,None,None,:]).repeat(1,len(features)*SAMPLE_COUNT,1)
			for k, feat_ind in enumerate(features):
				input_data[0,k*SAMPLE_COUNT:(k+1)*SAMPLE_COUNT,feat_ind] = torch.FloatTensor(np.random.choice(distribution[feat_ind,:], size=SAMPLE_COUNT))

			packed_input = torch.nn.utils.rnn.pack_padded_sequence(input_data, [1] *input_data.shape[1]).to(device)

			# Convert hidden state to larger batch size
			lstm_module.hidden = (lstm_module.hidden[0].repeat(1,input_data.shape[1],1), lstm_module.hidden[1].repeat(1,input_data.shape[1],1))
			# print("hidden before", lstm_module.hidden)
			output, _ = lstm_module(packed_input)
			sigmoided = torch.sigmoid(output[0,:,0]).detach().cpu()
			
			for k,feat_ind in enumerate(features):
				bin1 = (torch.round((PDF_FEATURE_BINS-1) * (input_data[0,:,feat_ind] - minmax[feat_ind][0]) / (minmax[feat_ind][1]-minmax[feat_ind][0]))).cpu().numpy().astype(int)
				bin2 = (torch.round((PDF_CONFIDENCE_BINS-1) * sigmoided)).cpu().numpy().astype(int)
				c = Counter(zip(bin1,bin2))
				bin1, bin2 = zip(*c.keys())
				joint_pdf = np.zeros([PDF_FEATURE_BINS,PDF_CONFIDENCE_BINS])
				joint_pdf[bin1,bin2] = list(c.values())
				mutinfos[i,feat_ind] += compute_mutinfo(joint_pdf)

			# Convert hidden state to batch size 1
			lstm_module.hidden = (lstm_module.hidden[0][:,0:1,:].contiguous(), lstm_module.hidden[1][:,0:1,:].contiguous())
			# print("hidden before", lstm_module.hidden)
			lstm_module.forgetting = False
			packed_input = torch.nn.utils.rnn.pack_padded_sequence(torch.FloatTensor(flow[i,:][None,None,:]), [1]).to(device)
			
			output, _ = lstm_module(packed_input)

	#  mutinfos = [[ compute_mutinfo(joint_pdf[i,j,:,:]) for i in range(opt.maxLength) ] for j,_ in enumerate(features) ]
	with open(opt.dataroot[:-7] + '_individual_mutinfos.pickle', 'wb') as f:
		pickle.dump((features, mutinfos/flow_lengths[:,None]), f)
	print("It took {} seconds per sample".format((time.time()-start_iterating)/len(test_data)))


def adv_internal(in_training = False, attack_types_which_are_not_investigated_anymore=[]):
	# FIXME: They suggest at least 10000 iterations with some specialized optimizer (Adam)
	# with SGD we probably need even more.
	if opt.advMethod == 'fgsm':
		ITERATION_COUNT = 1
	else:
		ITERATION_COUNT = 10 if in_training else opt.iterationCount

	lstm_module.train()

	# generate adversarial samples using Carlini Wagner method
	n_fold = opt.nFold
	fold = opt.fold

	if not opt.canManipulateBothDirections:
		bidirectional_categories = [torch.FloatTensor([mapping[key]]).to(device) for category in ['Botnet','Backdoors'] if category in categories_mapping for key in categories_mapping[category] ]

	#initialize sample
	train_indices, test_indices = get_nth_split(dataset, n_fold, fold)
	indices = train_indices if in_training else test_indices
	subset_with_all_traffic = torch.utils.data.Subset(dataset, indices)

	feature_ranges = get_feature_ranges(subset_with_all_traffic, sampling_density=2)

	zero_scaled = (0 - means[4])/stds[4]

	orig_indices, attack_indices = zip(*[(orig, i) for orig, i in zip(indices, range(len(subset_with_all_traffic))) if subset_with_all_traffic[i][1][0,0] == 1 and subset_with_all_traffic[i][2][0,0].item() not in attack_types_which_are_not_investigated_anymore])

	subset = torch.utils.data.Subset(dataset, orig_indices)
	print("len(subset)", len(subset))

	loader = torch.utils.data.DataLoader(subset, batch_size=opt.batchSize, shuffle=False, collate_fn=custom_collate)

	# lengths = torch.LongTensor([len(seq) for seq in batch_x])
	# packed = torch.nn.utils.rnn.pack_sequence(batch_x, enforce_sorted=False)

	#optimizer = optim.SGD([sample], lr=opt.lr)

	# iterate until done
	finished_adv_samples = [None] * len(subset)
	finished_categories = [ item[2][0,0] for item in subset ]

	zero_tensor = torch.FloatTensor([0]).to(device)

	samples = 0
	distances = []
	if in_training:
		# repeat forever
		sample_generator = itertools.chain.from_iterable(( enumerate(loader) for _ in itertools.count()))
		if opt.net:
			try:
				with open(adv_filename(), 'rb') as f:
					finished_adv_samples = pickle.load(f)
			except FileNotFoundError:
				print ('WARNING: failed to load adversarial flows file')
	else:
		sample_generator = enumerate(loader)

	for sample_index, (input_data,labels,input_categories) in sample_generator:
		# print("sample", sample_index)
		total_sample = samples % len(subset)
		samples += input_data.sorted_indices.shape[0]

		if opt.advMethod == "cw":
			optimizer = optim.SGD([input_data.data], lr=opt.lr)
		else:
			optimizer = optim.SGD(lstm_module.parameters(), lr=opt.lr)
			criterion = nn.BCEWithLogitsLoss(reduction="mean")

		orig_batch = input_data.data.clone()
		orig_batch_padded, orig_batch_lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)
		orig_batch_padded, orig_batch_lengths = orig_batch_padded.detach(), orig_batch_lengths.detach()
		if opt.order != 1:
			orig_batch_unpadded = unpad_padded_sequences(orig_batch_padded, orig_batch_lengths)

		if finished_adv_samples[-1] is not None:
			numpy_seqs = finished_adv_samples[total_sample:(total_sample+input_data.sorted_indices.shape[0])]
			new_data = collate_things([ torch.FloatTensor(seq) for seq in numpy_seqs], False).data
			assert input_data.data.shape == new_data.shape
			input_data.data.data = new_data
		input_data.data.requires_grad = True

		seqs, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)

		if opt.allowIATReduction:
			same_direction_mask = torch.cat((seqs[:1,:,5]!=seqs[:1,:,5], seqs[1:,:,5] == seqs[:-1,:,5]))
			same_direction_mask = torch.nn.utils.rnn.pack_padded_sequence(same_direction_mask, lengths, enforce_sorted=False).data.data
		else:
			same_direction_mask = False

		if not opt.canManipulateBothDirections:
			cats, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_categories)

			forward_direction = seqs[0:1,:,5].repeat(seqs.shape[0],1)

			index_tensor = torch.arange(0, seqs.shape[0], dtype=torch.int64).unsqueeze(1).repeat(1, seqs.shape[1])

			selection_tensor = lengths.unsqueeze(0).repeat(index_tensor.shape[0], 1)-1

			# print("index_tensor.shape", index_tensor.shape, "selection_tensor.shape", selection_tensor.shape)

			orig_mask = (index_tensor <= selection_tensor).byte().to(device)
			# mask_exact = (index_tensor == selection_tensor).byte().to(device)
			wrong_direction = (seqs[:,:,5]!=forward_direction).byte()
			matching_cats = [(cats==bidirectional_cat).squeeze().byte() for bidirectional_cat in bidirectional_categories]
			# print("matching_cats.shape", [item.shape for item in matching_cats])
			not_bidirectional = ~reduce(lambda acc, x: acc | x, matching_cats, torch.ByteTensor([False]).to(device))
			if not_bidirectional.shape != orig_mask.shape:
				not_bidirectional = not_bidirectional[:,None]
				not_bidirectional = not_bidirectional.repeat(1,orig_mask.shape[-1])
			# print(orig_mask.shape, wrong_direction.shape, not_bidirectional.shape)
			mask = orig_mask & wrong_direction & not_bidirectional

			# print("Batch: {}, orig_mask: {}, wrong_direction: {}, not_bidirectional: {}, mask: {}".format(sample_index, (torch.sum(orig_mask)), (torch.sum(wrong_direction & orig_mask)), (torch.sum(not_bidirectional & orig_mask)), (torch.sum(mask))))

			# print("Batch: {}, wrong_direction: {}, not_bidirectional: {}, invalid: {}".format(sample_index, float(torch.sum(wrong_direction & orig_mask)/torch.sum(orig_mask, dtype=torch.float32)), float(torch.sum(not_bidirectional & orig_mask)/torch.sum(orig_mask, dtype=torch.float32)), float(torch.sum(mask)/torch.sum(orig_mask, dtype=torch.float32))))

		index_tensor = torch.arange(0, seqs.shape[0], dtype=torch.int64).unsqueeze(1).unsqueeze(2).repeat(1, seqs.shape[1], 1)

		selection_tensor = lengths.unsqueeze(0).unsqueeze(2).repeat(index_tensor.shape[0], 1, index_tensor.shape[2])-1

		mask_pgd = (index_tensor <= selection_tensor).byte().to(device)
		labels, _ = torch.nn.utils.rnn.pad_packed_sequence(labels)

		for i in range(ITERATION_COUNT):

			# print("iterating", i)
			# samples += len(input_data)
			optimizer.zero_grad()
			lstm_module.init_hidden(input_data.sorted_indices.shape[0])

			# actual_input = torch.FloatTensor(input_tensor[:,:,:-1]).to(device)

			# print("input_data.data.shape", input_data.data.shape)
			#s_squeezed = torch.nn.utils.rnn.pack_padded_sequence(torch.unsqueeze(sample, 1), [sample.shape[0]])
			output, seq_lens = lstm_module(input_data)
			# output_padded, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)

			if opt.advMethod == "cw":

				if opt.order != 1:
					seqs, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)
					unpadded_seqs = unpad_padded_sequences(seqs, lengths)
					distance = torch.stack([torch.dist(orig_batch_unpadded_item, unpadded_seqs_item, p=opt.order) for orig_batch_unpadded_item, unpadded_seqs_item in zip(orig_batch_unpadded, unpadded_seqs)]).sum()
				else:
					distance = torch.dist(orig_batch, input_data.data, p=opt.order)
				regularizer = opt.tradeoff*torch.max(output, zero_tensor).sum()
				criterion = distance + regularizer
				if opt.penaltyTradeoff > 0:
					seqs, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)
					penalty = opt.penaltyTradeoff*((seqs[:,:,4].sum(0) - orig_batch_padded[:,:,4].sum(0))**2).sum()
					criterion += penalty
				criterion.backward()

				# only consider lengths and iat
				input_data.data.grad[:,:3] = 0
				input_data.data.grad[:,5:] = 0
				optimizer.step()
			else:

				loss = criterion(output[mask_pgd].view(-1), labels[mask_pgd].view(-1))
				loss.backward()

				gradient = input_data.data.grad
				gradient[:,:3] = 0
				gradient[:,5:] = 0
				if opt.advMethod == 'fgsm':
					input_data.data.data = input_data.data.data + opt.tradeoff*gradient.sign()
				else:
					input_data.data.data += opt.lr * gradient
					delta = input_data.data.data - orig_batch.data
					proj_mask = delta.abs() > opt.tradeoff
					input_data.data.data[proj_mask] = orig_batch.data[proj_mask] + opt.tradeoff * delta[proj_mask].sign()

			# Packet lengths cannot become smaller than original
			packet_mask = input_data.data[:,3] < orig_batch[:,3]
			input_data.data.data[packet_mask,3] = orig_batch[packet_mask,3]

			# # Packet lengths cannot become larger than the maximum. Should be around 1500 bytes usually... NOTE: Apparently packets are commonly larger than 1500 bytes so this is not enforcable like this :/
			# mask = input_data.data[:,3] > maximum_length
			# input_data.data.data[mask,3] = orig_batch[mask,3]

			# XXX: This is experimentally removed
			# # IAT cannot become smaller than 0 when the preceding packet went in the same direction
			iat_mask = (input_data.data[:,4] < zero_scaled) & same_direction_mask
			input_data.data.data[iat_mask,4] = float(zero_scaled)

			# XXX: This is experimentally added
			iat_mask = (input_data.data[:,4] < orig_batch[:,4]) & ~same_direction_mask
			input_data.data.data[iat_mask,4] = orig_batch[iat_mask,4]

			# Can only manipulate attacker direction except for botnets where we can control both sides
			if not opt.canManipulateBothDirections:
				seqs, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)

				seqs[mask] = orig_batch_padded[mask]
				input_data.data.data = torch.nn.utils.rnn.pack_padded_sequence(seqs, lengths, enforce_sorted=False).data.data

			seqs, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)

			seqs[0,:,4] = zero_scaled
			input_data.data.data = torch.nn.utils.rnn.pack_padded_sequence(seqs, lengths, enforce_sorted=False).data.data

		seqs, lengths = torch.nn.utils.rnn.pad_packed_sequence(input_data)

		adv_samples = [ seqs[:lengths[batch_index],batch_index,:].detach().cpu().numpy() for batch_index in range(seqs.shape[1]) ]

		finished_adv_samples[total_sample:(total_sample+len(adv_samples))] = adv_samples
		if in_training:
			distances.append(torch.dist(orig_batch, input_data.data, p=1)/seqs.shape[1])
			# keep iterations for adversarial flows about the same as iterations for training
			if finished_adv_samples[-1] is not None and len(distances) * ITERATION_COUNT >= len(subset_with_all_traffic) / opt.batchSize:
				yield finished_adv_samples, finished_categories, sum(distances)/len(distances)
				distances = []

	assert len(finished_adv_samples) == len(subset), "len(finished_adv_samples): {}, len(subset): {}".format(len(finished_adv_samples), len(subset))

	original_dataset = OurDataset(*zip(*[[subitem.numpy() for subitem in item] for item in list(subset)]))
	subset = OurDataset(*zip(*[[subitem.numpy() for subitem in item] for item in list(subset)]))
	subset.data = finished_adv_samples

	original_results = eval_nn(original_dataset)
	results = eval_nn(subset)

	assert len(results) == len(subset)

	print("Tradeoff: {}".format(opt.tradeoff))
	print("Number of attack samples: {}".format(len(subset)))
	print("Average confidence on original packets: {}".format(np.mean(numpy_sigmoid(np.concatenate([np.array(item) for item in original_results], axis=0)))))
	print("Average confidence on packets: {}".format(np.mean(numpy_sigmoid(np.concatenate([np.array(item) for item in results], axis=0)))))
	print("Ratio of successful adversarial attacks on packets: {}".format(1-np.mean(np.round(numpy_sigmoid(np.concatenate([np.array(item) for item in results], axis=0))))))
	print("Average confidence on original flows: {}".format(np.mean(numpy_sigmoid(np.array([item[-1] for item in original_results])))))
	print("Average confidence on flows: {}".format(np.mean(numpy_sigmoid(np.array([item[-1] for item in results])))))
	print("Ratio of successful adversarial attacks on flows: {}".format(1-np.mean(np.round(numpy_sigmoid(np.array([item[-1] for item in results]))))))

	attack_numbers = mapping.values()

	orig_flows_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	modified_flows_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	orig_results_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	results_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	sample_indices_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	for orig_index, (orig_flow,_,cat), (adv_flow,_,_), orig_result, result in zip(orig_indices, original_dataset, subset, original_results, results):
		assert len(orig_flow) > 0
		correct_cat = int(cat[0][0])
		orig_flows_by_attack_number[correct_cat].append(orig_flow)
		modified_flows_by_attack_number[correct_cat].append(adv_flow)
		orig_results_by_attack_number[correct_cat].append(orig_result)
		results_by_attack_number[correct_cat].append(result)
		sample_indices_by_attack_number.append(orig_index)

	reverse_mapping = {v: k for k, v in mapping.items()}
	for attack_number, (per_attack_orig, per_attack_modified, per_attack_orig_results, per_attack_results) in enumerate(zip(orig_flows_by_attack_number, modified_flows_by_attack_number, orig_results_by_attack_number, results_by_attack_number)):
		if len(per_attack_results) <= 0:
			continue
		per_packet_orig_accuracy = (np.mean(np.round(numpy_sigmoid(np.concatenate([np.array(item) for item in per_attack_orig_results], axis=0)))))
		per_packet_accuracy = (np.mean(np.round(numpy_sigmoid(np.concatenate([np.array(item) for item in per_attack_results], axis=0)))))
		per_flow_orig_accuracy = (np.mean(np.round(numpy_sigmoid(np.array([item[-1] for item in per_attack_orig_results])))))
		per_flow_accuracy = (np.mean(np.round(numpy_sigmoid(np.array([item[-1] for item in per_attack_results])))))
		# TODO: l1 or l2 norm?
		dist = np.array([np.linalg.norm((per_attack_orig_item-per_attack_modified_item).flatten(), ord=1).mean() for per_attack_orig_item, per_attack_modified_item in zip(per_attack_orig, per_attack_modified)]).mean()

		print("Attack type: {}; number of samples: {}, average dist: {}, packet confidence: {}/{}, flow confidence: {}/{}".format(reverse_mapping[attack_number], len(per_attack_results), dist, per_packet_accuracy, per_packet_orig_accuracy, per_flow_accuracy, per_flow_orig_accuracy))

	file_name = opt.dataroot[:-7]+"_adv_{}{}_outcomes_{}_{}.pickle".format(opt.tradeoff, "_notBidirectional" if not opt.canManipulateBothDirections else "", opt.fold, opt.nFold)
	results_dict = {"results_by_attack_number": results_by_attack_number, "orig_results_by_attack_number": orig_results_by_attack_number, "modified_flows_by_attack_number": modified_flows_by_attack_number, "orig_flows_by_attack_number": orig_flows_by_attack_number}
	with open(file_name, "wb") as f:
		pickle.dump(results_dict, f)

	if not in_training:
		yield results_dict

def adv():
	# hack for running the function although it's a generator
	list(adv_internal(False))

def adv_until_less_than_half():
	MAX = 100
	SCALING = 0.5
	THRESHOLD = 0.5
	i = 0
	prev_results = []
	prev_flows = []
	orig_results = None
	orig_flows = None
	prev_ratios = []
	next_filter = []
	while True:
		# FIXME: Hack: Shouldn't assign input parameter I guess
		opt.tradeoff = i*SCALING
		results_dict = list(adv_internal(False, next_filter))[0]
		modified_flows_by_attack, modified_results_by_attack, original_flows_by_attack, original_results_by_attack = results_dict["modified_flows_by_attack_number"], results_dict["results_by_attack_number"], results_dict["orig_flows_by_attack_number"], results_dict["orig_results_by_attack_number"]
		ratio_modified_by_attack_number = np.array([np.mean(numpy_sigmoid(np.array([item[-1] for item in modified_results]))) if len(modified_results)>0 else -float("inf") for modified_results in modified_results_by_attack])
		next_filter = [index for index, item in enumerate(ratio_modified_by_attack_number) if item <= THRESHOLD]
		if i==0:
			orig_results = original_results_by_attack
			orig_flows = original_flows_by_attack
		prev_results.append(modified_results_by_attack)
		prev_flows.append(modified_flows_by_attack)
		prev_ratios.append(ratio_modified_by_attack_number)
		print("i", i, "ratios", ratio_modified_by_attack_number)
		if i+1==MAX or (ratio_modified_by_attack_number <= THRESHOLD).all() or (len(prev_ratios) > 1 and (prev_ratios[-2] <= prev_ratios[-1]).all()):
			break
		i += 1

	reverse_mapping = {v: k for k, v in mapping.items()}
	distances_packets = [None]*len(orig_results)
	distances_flows = [None]*len(orig_results)
	final_ratios = [None]*len(orig_results)
	for i in range(len(prev_results)):
		for attack_index, ratio in enumerate(prev_ratios[i]):
			if ratio > -float("inf"):
				final_ratios[attack_index] = ratio

				successfully_changed_flows_mask = (np.round(numpy_sigmoid(np.array([item[-1] for item in prev_results[i][attack_index]]))) == 0).flatten()
				# TODO: l1 or l2 norm?
				distances = np.array([np.linalg.norm((orig_flows[attack_index][flow_index]-flow).flatten(), ord=1) for flow_index, flow in enumerate(prev_flows[i][attack_index])])
				argsorted_distances = np.argsort(distances)
				correct_indices = argsorted_distances[successfully_changed_flows_mask[argsorted_distances]]
				lower_part = correct_indices[:int(math.ceil(len(distances)*min(ratio, THRESHOLD)))]

				distances_per_packet = [dist/len(flow) for dist, flow in zip(distances[lower_part], prev_flows[i][attack_index])]
				distances_flows[attack_index] = np.mean(distances[lower_part])
				distances_packets[attack_index] = np.mean(distances_per_packet)

	for attack_index in range(len(distances_packets)):
		if len(orig_results[attack_index]) <= 0:
			continue
		print(
			"attack_type", reverse_mapping[attack_index],
			"ratio", final_ratios[attack_index],
			"flow_accuracy", np.mean(numpy_sigmoid(np.array([item[-1] for item in orig_results[attack_index]]))),
			"packet_accuracy", np.mean(numpy_sigmoid(np.array([sublist for l in orig_results[attack_index] for sublist in l]))),
			"flow_distance", distances_flows[attack_index],
			"packet_distance", distances_packets[attack_index]
		)

def eval_nn(data):

	lstm_module.eval()

	results = []

	loader = torch.utils.data.DataLoader(data, batch_size=opt.batchSize, shuffle=False, collate_fn=lambda x: custom_collate(x, (True, False, False)))

	for (input_data,) in loader:

		lstm_module.init_hidden(opt.batchSize)

		output, seq_lens = lstm_module(input_data)

		# Data is (Sequence Index, Batch Index, Feature Index)
		for batch_index in range(output.shape[1]):
			flow_length = seq_lens[batch_index]
			#flow_input = input_data[:flow_length,batch_index,:].detach().cpu().numpy()
			flow_output = output[:flow_length,batch_index,:].detach().cpu().numpy()

			results.append(flow_output)

	return results

def get_feature_ranges(dataset, sampling_density=100):
	features = []
	# iat & length
	for feat_name, feat_ind in zip(["length", "iat"], [3, 4]):
		feat_min = min( (sample[0][i,feat_ind] for sample in dataset for i in range(sample[0].shape[0])))
		feat_max = max( (sample[0][i,feat_ind] for sample in dataset for i in range(sample[0].shape[0])))
		features.append((feat_ind,np.linspace(feat_min, feat_max, sampling_density)))

		# print("feature", feat_name, "min", feat_min, "max", feat_max, "min_rescaled", feat_min*stds[feat_ind] + means[feat_ind], "max_rescaled", feat_max*stds[feat_ind] + means[feat_ind])
	return features

def get_feature_ranges_from_adv(sampling_density=100):
	features = []

	with open(opt.pathToAdvOutput, "rb") as f:
		adv_loaded = pickle.load(f)
		adv_modified_flows_by_attack_number = adv_loaded["modified_flows_by_attack_number"]
		adv_orig_flows_by_attack_number = adv_loaded["orig_flows_by_attack_number"]

	# iat & length
	for attack_type in range(len(adv_modified_flows_by_attack_number)):

		features.append([])
		if len(adv_modified_flows_by_attack_number[attack_type]) == 0:
			continue
		print("attack_type", attack_type)
		for feat_name, feat_ind in zip(["length", "iat"], [3, 4]):
			feat_min_orig = min( sample[i][feat_ind] for sample in adv_orig_flows_by_attack_number[attack_type] for i in range(len(sample)))
			feat_max_orig = max( sample[i][feat_ind] for sample in adv_orig_flows_by_attack_number[attack_type] for i in range(len(sample)))

			feat_min_modified = min( sample[i][feat_ind] for sample in adv_modified_flows_by_attack_number[attack_type] for i in range(len(sample)))
			feat_max_modified = max( sample[i][feat_ind] for sample in adv_modified_flows_by_attack_number[attack_type] for i in range(len(sample)))

			# print("feat_min_orig", feat_min_orig*stds[feat_ind] + means[feat_ind], "feat_min_modified", feat_min_modified*stds[feat_ind] + means[feat_ind])
			feat_min = min(feat_min_orig, feat_min_modified)
			# print("feat_max_orig", feat_max_orig*stds[feat_ind] + means[feat_ind], "feat_max_modified", feat_max_modified*stds[feat_ind] + means[feat_ind])
			feat_max = max(feat_max_orig, feat_max_modified)

			grid = np.linspace(feat_min, feat_max, sampling_density)
			# print("grid", max(grid*stds[feat_ind] + means[feat_ind]))
			features[attack_type].append((feat_ind,grid))

		# print("feature", feat_name, "min", feat_min, "max", feat_max, "min_rescaled", feat_min*stds[feat_ind] + means[feat_ind], "max_rescaled", feat_max*stds[feat_ind] + means[feat_ind])
	return features

def pred_plots():
	OUT_DIR='pred_plots'
	os.makedirs(OUT_DIR, exist_ok=True)

	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	subset = torch.utils.data.Subset(dataset, test_indices)

	features = get_feature_ranges(subset)

	attack_numbers = mapping.values()

	results_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	sample_indices_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	start_iterating = time.time()
	# have_categories = collections.defaultdict(int)
	for real_ind, sample_ind in zip(test_indices, range(len(subset))):
		# print("index", sample_ind)
		# if have_categories[cat] == SAMPLES_PER_ATTACK:
		# 	continue
		# have_categories[cat] += 1

		flow, _, flow_categories = subset[sample_ind]
		cat = int(flow_categories[0,0])

		lstm_module.init_hidden(1)

		predictions = np.zeros((flow.shape[0],))
		mins = np.ones((len(features),flow.shape[0],))
		maxs = np.zeros((len(features),flow.shape[0],))

		for i in range(flow.shape[0]):

			lstm_module.forgetting = True

			input_data = torch.FloatTensor(flow[i,:][None,None,:]).repeat(1,len(features)*len(features[0][1]),1)
			for k, (feat_ind, values) in enumerate(features):

				for j in range(values.size):
					input_data[0,k*values.size+j,feat_ind] = values[j]

			packed_input = torch.nn.utils.rnn.pack_padded_sequence(input_data, [1] *input_data.shape[1]).to(device)

			lstm_module.hidden = (lstm_module.hidden[0].repeat(1,input_data.shape[1],1), lstm_module.hidden[1].repeat(1,input_data.shape[1],1))
			# print("hidden before", lstm_module.hidden)
			output, _ = lstm_module(packed_input)
			sigmoided = torch.sigmoid(output[0,:,0]).detach().cpu().tolist()

			for k, (feat_ind, values) in enumerate(features):
				for j in range(values.size):
					mins[k,i] = min(mins[k,i], *sigmoided[k*values.size:(k+1)*values.size])
					maxs[k,i] = max(maxs[k,i], *sigmoided[k*values.size:(k+1)*values.size])

			lstm_module.hidden = (lstm_module.hidden[0][:,0:1,:].contiguous(), lstm_module.hidden[1][:,0:1,:].contiguous())
			# print("hidden before", lstm_module.hidden)
			lstm_module.forgetting = False
			packed_input = torch.nn.utils.rnn.pack_padded_sequence(torch.FloatTensor(flow[i,:][None,None,:]), [1]).to(device)
			output, _ = lstm_module(packed_input)
			predictions[i] = torch.sigmoid(output[0,0,0])

		results_by_attack_number[cat].append(np.vstack((predictions,mins,maxs)))
		sample_indices_by_attack_number[cat].append(real_ind)

	print("It took {} seconds per sample".format((time.time()-start_iterating)/len(subset)))
	file_name = opt.dataroot[:-7]+"_pred_plots_outcomes_{}_{}.pickle".format(opt.fold, opt.nFold)
	with open(file_name, "wb") as f:
		pickle.dump({"results_by_attack_number": results_by_attack_number, "sample_indices_by_attack_number": sample_indices_by_attack_number}, f)

def pred_plots2():
	OUT_DIR='pred_plots2'
	os.makedirs(OUT_DIR, exist_ok=True)

	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	subset = torch.utils.data.Subset(dataset, test_indices)

	if opt.pathToAdvOutput == "":
		features = get_feature_ranges(subset, sampling_density=100)
	else:
		features = get_feature_ranges_from_adv(sampling_density=100)
	# print("features", features)

	attack_numbers = mapping.values()

	results_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	flows_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	result_ranges_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	sample_indices_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	start_iterating = time.time()
	# have_categories = collections.defaultdict(int)
	for real_ind, sample_ind in zip(test_indices, range(len(subset))):
		if sample_ind % 1000==0:
			print("index", sample_ind)
		# if have_categories[cat] == SAMPLES_PER_ATTACK:
		# 	continue
		# have_categories[cat] += 1

		flow, _, flow_categories = subset[sample_ind]
		cat = int(flow_categories[0,0])

		lstm_module.init_hidden(1)

		predictions = np.zeros((flow.shape[0],))
		prediction_ranges = np.zeros((flow.shape[0], max([len(item) for item in features]), max([(len(item[0][1])-1) if len(item) > 0 else 0 for item in features])))

		for i in range(flow.shape[0]):

			lstm_module.forgetting = True

			input_data = torch.FloatTensor(flow[i,:][None,None,:]).repeat(1,max([len(item) for item in features])*max([(len(item[0][1])-1) if len(item) > 0 else 0 for item in features]),1)
			for k, (feat_ind, values) in enumerate(features[cat] if opt.pathToAdvOutput!="" else features):

				for j in range(values.size-1):
					# print("input_data.shape", input_data.shape, "k*values.size+j", k*values.size+j)
					input_data[0,k*(values.size-1)+j,feat_ind] = (values[j]+values[j+1])/2

			packed_input = torch.nn.utils.rnn.pack_padded_sequence(input_data, [1] *input_data.shape[1]).to(device)

			# print("input_data.shape", input_data.shape)
			# print("hidden before before", [item.shape for item in lstm_module.hidden])
			lstm_module.hidden = (lstm_module.hidden[0].repeat(1,input_data.shape[1],1), lstm_module.hidden[1].repeat(1,input_data.shape[1],1))
			# print("hidden before", [item.shape for item in lstm_module.hidden])
			output, _ = lstm_module(packed_input)
			sigmoided = torch.sigmoid(output[0,:,0]).detach().cpu().tolist()

			for k, (feat_ind, values) in enumerate(features[cat] if opt.pathToAdvOutput!="" else features):
				for j in range(values.size-1):
					prediction_ranges[i,k,:] = sigmoided[k*(values.size-1):(k+1)*(values.size-1)]

			lstm_module.hidden = (lstm_module.hidden[0][:,0:1,:].contiguous(), lstm_module.hidden[1][:,0:1,:].contiguous())
			# print("hidden before", lstm_module.hidden)
			lstm_module.forgetting = False
			packed_input = torch.nn.utils.rnn.pack_padded_sequence(torch.FloatTensor(flow[i,:][None,None,:]), [1]).to(device)
			output, _ = lstm_module(packed_input)
			predictions[i] = torch.sigmoid(output[0,0,0])

		results_by_attack_number[cat].append(predictions)
		flows_by_attack_number[cat].append(flow.detach().cpu().numpy())
		result_ranges_by_attack_number[cat].append(prediction_ranges)
		sample_indices_by_attack_number[cat].append(real_ind)

		# assert result_ranges_by_attack_number[cat][-1].__class__.__name__=="ndarray" and flows_by_attack_number[cat][-1].__class__.__name__=="ndarray" and result_ranges_by_attack_number[cat][-1].__class__.__name__=="ndarray" and sample_indices_by_attack_number[cat][-1].__class__.__name__=="int", "{}, {}, {}, {}".format(result_ranges_by_attack_number[cat][-1].__class__.__name__, flows_by_attack_number[cat][-1].__class__.__name__, result_ranges_by_attack_number[cat][-1].__class__.__name__, sample_indices_by_attack_number[cat][-1].__class__.__name__)

	print("It took {} seconds per sample".format((time.time()-start_iterating)/len(subset)))
	file_name = opt.dataroot[:-7]+"_pred_plots2_outcomes{}_{}_{}.pickle".format("_"+opt.pathToAdvOutput if opt.pathToAdvOutput!="" else "", opt.fold, opt.nFold)
	with open(file_name, "wb") as f:
		pickle.dump({"results_by_attack_number": results_by_attack_number, "flows_by_attack_number": flows_by_attack_number, "result_ranges_by_attack_number": result_ranges_by_attack_number, "sample_indices_by_attack_number": sample_indices_by_attack_number, "features": features}, f)

def pdp():

	feature_names = ["srcPort", "dstPort"]
	n_fold = opt.nFold
	fold = opt.fold
	lstm_module.eval()

	_, test_indices = get_nth_split(dataset, n_fold, fold)
	subset = torch.utils.data.Subset(dataset, test_indices)

	attack_numbers = mapping.values()

	results_by_attack_number = [None for _ in range(min(attack_numbers), max(attack_numbers)+1)]
	feature_values_by_attack_number = [list() for _ in range(min(attack_numbers), max(attack_numbers)+1)]

	minmax = {feat_ind: (min((sample[0,feat_ind] for sample in x)), max((sample[0,feat_ind] for sample in x))) for feat_ind in [0,1] }
	# TODO: consider fold
	for attack_number in range(max(attack_numbers)+1):

		matching = [item for item in subset if int(item[2][0,0]) == attack_number]
		if len(matching) <= 0:
			break
		matching = matching[:1000]
		good_subset = OurDataset(*zip(*matching))
		print("len(good_subset)", len(good_subset))

		print("attack_number", attack_number)
		results_for_attack_type = []
		for feat_name, feat_ind in zip(feature_names, (0, 1)):
			feature_values_by_attack_number[attack_number].append(np.array([item[0][0,feat_ind] for item in matching])*stds[feat_ind] + means[feat_ind])

			feat_min, feat_max = minmax[feat_ind]

			values = np.linspace(feat_min, feat_max, 100)

			# subset = [ torch.FloatTensor(sample) for sample in x[:opt.batchSize] ]

			pdp = np.zeros([values.size])

			for i in range(values.size):
				# good_subset.data consists of torch tensors. We are therefore able to
				# modify the dataset directly using the return value of __getitem__().
				# This does not modify the global dataset, which holds the data as numpy
				# arrays.
				for index, sample in enumerate(good_subset):
					# if index % 1000 == 0:
					# 	print("attack_number", attack_number, "feat_name", feat_name, "index", index)
					for j in range(sample[0].shape[0]):
						sample[0][j,feat_ind] = values[i]
				outputs = eval_nn(good_subset)
				# TODO: avg. or end output?
				pdp[i] = np.mean( np.array([numpy_sigmoid(output[-1]) for output in outputs] ))

			rescaled = values * stds[feat_ind] + means[feat_ind]
			# os.makedirs(PDP_DIR, exist_ok=True)
			results_for_attack_type.append(np.vstack((rescaled,pdp)))
			# print("result.shape", result.shape)
			# np.save('%s/%s.npy' % (PDP_DIR, feat_name), result)

		else:
			results_by_attack_number[attack_number] = np.stack(results_for_attack_type)

	file_name = opt.dataroot[:-7]+"_pdp_outcomes_{}_{}.pickle".format(opt.fold, opt.nFold)
	with open(file_name, "wb") as f:
		pickle.dump({"results_by_attack_number": results_by_attack_number, "feature_names": feature_names, "feature_values_by_attack_number": feature_values_by_attack_number}, f)

def plot_histograms():
	rescaled = [item * stds + means for item in x[:opt.maxSize]]
	for i in range(rescaled[0].shape[-1]):
		plt.hist([subitem for item in rescaled for subitem in list(item[:,i])], bins=100)
		plt.title("Feature {}".format(i))
		plt.show()

def overwrite_manipulable_entries(seq, filler=-1):
		forward_direction = seq[0,5]

		wrong_direction = (seq[:,5]==forward_direction)
		seq[wrong_direction,:][:,3:5] = filler
		return seq

if __name__=="__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument('--canManipulateBothDirections', action='store_true', help='if the attacker can change packets in both directions of the flow')
	parser.add_argument('--dataroot', required=True, help='path to dataset')
	parser.add_argument('--normalizationData', default="", type=str, help='normalization data to use')
	parser.add_argument('--fold', type=int, default=0, help='fold to use')
	parser.add_argument('--nFold', type=int, default=3, help='total number of folds')
	parser.add_argument('--batchSize', type=int, default=128, help='input batch size')
	parser.add_argument('--net', default='', help="path to net (to continue training)")
	parser.add_argument('--function', default='train', help='the function that is going to be called')
	parser.add_argument('--manualSeed', default=0, type=int, help='manual seed')
	parser.add_argument('--maxLength', type=int, default=100, help='max length')
	parser.add_argument('--maxSize', type=int, default=sys.maxsize, help='limit of samples to consider')
#	parser.add_argument("--categoriesMapping", type=str, default="categories_mapping.json", help="mapping of attack categories; see parse.py")
	parser.add_argument('--removeChangeable', action='store_true', help='when training remove all features that an attacker could manipulate easily without changing the attack itself')
	parser.add_argument('--tradeoff', type=float, default=0.5, help='max length')
	parser.add_argument('--penaltyTradeoff', type=float, default=0, help='Tradeoff to enforce constant flow duration')
	parser.add_argument('--lr', type=float, default=10**(-2), help='learning rate')
	parser.add_argument('--advTraining', action='store_true', help='Train with adversarial flows')
	parser.add_argument('--allowIATReduction', action='store_true', help='allow reducing IAT below original value')
	parser.add_argument('--order', type=int, default=1, help='order of the norm for adversarial sample generation')
	parser.add_argument('--advMethod', type=str, default="cw", help='which adversarial samples method to use; options are: "cw", "pgd"')
	parser.add_argument('--iterationCount', type=int, default=100, help='number of iterations for creating adversarial samples')
	parser.add_argument('--pathToAdvOutput', type=str, default="", help='path to adv output to be used in pred_plots2')
	parser.add_argument('--averageFeaturesToPruneDuringTraining', type=int, default=-1, help='average number of features that should be "dropped out"; -1 to disable (default)')
	parser.add_argument('--mutinfo', action='store_true', help='also compute mutinfo during the feature_importance function')
	parser.add_argument('--hidden_size', type=int, default=512, help='number of neurons per layer')
	parser.add_argument('--n_layers', type=int, default=3, help='number of LSTM layers')
	parser.add_argument('--adjustFeatImpDistribution', action='store_true', help='adjust randomization feature importance distributions to a practically relevant shape')

	# parser.add_argument('--nSamples', type=int, default=1, help='number of items to sample for the feature importance metric')

	opt = parser.parse_args()
	print(opt)
	SEED = opt.manualSeed
	random.seed(SEED)
	np.random.seed(SEED)
	torch.manual_seed(SEED)

	with open (opt.dataroot, "rb") as f:
		all_data = pickle.load(f)

	with open(opt.dataroot[:-7]+"_categories_mapping.json", "r") as f:
		categories_mapping_content = json.load(f)
	categories_mapping, mapping = categories_mapping_content["categories_mapping"], categories_mapping_content["mapping"]
	assert min(mapping.values()) == 0

	# a few flows have have invalid IATs due to dataset issues. Sort those out.
	all_data = [item[:opt.maxLength,:] for item in all_data if np.all(item[:,4]>=0)]
	if opt.removeChangeable:
		all_data = [overwrite_manipulable_entries(item) for item in all_data]
	random.shuffle(all_data)
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

	batchSize = 1 if opt.function == 'pred_plots' else opt.batchSize # FIXME Max: Why? What's wrong?
	lstm_module = OurLSTMModule(x[0].shape[-1], y[0].shape[-1], opt.hidden_size, opt.n_layers, batchSize, device).to(device)

	if opt.net != '':
		print("Loading", opt.net)
		lstm_module.load_state_dict(torch.load(opt.net, map_location=device))

	globals()[opt.function]()
