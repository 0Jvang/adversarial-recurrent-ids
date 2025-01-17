#!/usr/bin/env python3

import matplotlib.pyplot as plt
import numpy as np
import sys
import os
import json
import pickle
from learn import numpy_sigmoid

DIR_NAME = "plots/plot_adv"

dataroot_basename = sys.argv[1].split('_')[0]

with open(dataroot_basename + "_categories_mapping.json", "r") as f:
	categories_mapping_content = json.load(f)
categories_mapping, mapping = categories_mapping_content["categories_mapping"], categories_mapping_content["mapping"]
reverse_mapping = {v: k for k, v in mapping.items()}
# print("reverse_mapping", reverse_mapping)

with open(dataroot_basename+"_full_no_ttl_normalization_data.pickle", "rb") as f:
	means, stds = pickle.load(f)

# TODO: Implement for more than one adv output so that different tradeoffs can be compared.
file_name = sys.argv[1]
with open(file_name, "rb") as f:
	loaded = pickle.load(f)
results_by_attack_number = loaded["results_by_attack_number"]
orig_results_by_attack_number = loaded["orig_results_by_attack_number"]
modified_flows_by_attack_number = loaded["modified_flows_by_attack_number"]
orig_flows_by_attack_number = loaded["orig_flows_by_attack_number"]

colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
ORDERING = ["original", "adversarial"]
FEATURE_NAMES = ["packet length", "iat"]

for attack_type, (results_by_attack_number_item, orig_results_by_attack_number_item, modified_flows_by_attack_number_item, orig_flows_by_attack_number_item) in enumerate(zip(results_by_attack_number, orig_results_by_attack_number, modified_flows_by_attack_number, orig_flows_by_attack_number)):

	assert len(results_by_attack_number_item) == len(orig_results_by_attack_number_item) == len(modified_flows_by_attack_number_item) == len(orig_flows_by_attack_number_item)
	if len(results_by_attack_number_item) <= 0:
		continue

	stacked_original = [np.concatenate((np.array(orig_flow), np.array(orig_result)), axis=-1) for orig_flow, orig_result in zip(orig_flows_by_attack_number_item, orig_results_by_attack_number_item)]
	stacked_modified = [np.concatenate((np.array(modified_flow), np.array(modified_result)), axis=-1) for modified_flow, modified_result in zip(modified_flows_by_attack_number_item, results_by_attack_number_item)]

	seqs = [np.stack((orig, modified)) for orig, modified in zip(stacked_original, stacked_modified)]

	# Filter good seqs where the adversarial attack succeeded.
	filtered_seqs = [item for item in seqs if int(np.round(np.mean(numpy_sigmoid(item[0,-1:,-1])))) == 1 and int(np.round(np.mean(numpy_sigmoid(item[1,-1:,-1])))) == 0]

	print("Original seqs", len(seqs), "filtered seqs", len(filtered_seqs))
	seqs = filtered_seqs

	if len(filtered_seqs) <= 0:
		continue

	seqs = sorted(seqs, key=lambda x: x.shape[1], reverse=True)
	max_length = seqs[0].shape[1]
	print("max_length", max_length)

	values_by_length = []

	for i in range(max_length):
		values_by_length.append([])
		for seq in seqs:
			if seq.shape[1] < i+1:
				break

			values_by_length[i].append(seq[:,i:i+1,:])

	for i in range(len(values_by_length)):
		values_by_length[i] = np.concatenate(values_by_length[i], axis=1)

	flow_means = np.array([np.mean(item, axis=1) for item in values_by_length])
	medians = np.array([np.median(item, axis=1) for item in values_by_length])

	all_legends = []
	assert len(flow_means[1].shape) == 2
	fig, ax1 = plt.subplots()
	ax2 = ax1.twinx()

	for feature_index_from_zero, (feature_name, feature_index, ax) in enumerate(zip(FEATURE_NAMES, (3, 4), (ax1, ax2))):
		ax.set_ylabel(feature_name, color=colors[feature_index_from_zero])
		for adv_real_index in range(flow_means.shape[1]):
			correct_linestyle = "solid" if adv_real_index==0 else "dashed"
			legend = "{}, {}".format(ORDERING[adv_real_index], feature_name)
			ret = ax.plot(range(max_length), flow_means[:,adv_real_index,feature_index]*stds[feature_index]+means[feature_index], label=legend, linestyle=correct_linestyle, color=colors[feature_index_from_zero])
			all_legends += ret

	plt.title(reverse_mapping[attack_type])
	all_labels = [item.get_label() for item in all_legends]
	ax1.legend(all_legends, all_labels)
	plt.xlabel('Sequence index')
	plt.tight_layout()

	os.makedirs(DIR_NAME, exist_ok=True)
	plt.savefig(DIR_NAME+'/{}_{}_{}.pdf'.format(file_name.split("/")[-1], attack_type, reverse_mapping[attack_type].replace("/", "-").replace(":", "-")))
	plt.clf()



