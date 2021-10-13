import json
import pickle
from tqdm import tqdm  # For nice progress bar!
import os
import numpy as np
import matplotlib.pyplot as plt
import time

import torch
from torch.optim.lr_scheduler import StepLR, ReduceLROnPlateau
from torch.nn.utils import clip_grad_norm_, clip_grad_value_
from torch import optim
from torch.utils.data import DataLoader
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
torch.autograd.set_detect_anomaly(True)

import param
from mdl.fnn import FNN
from mdl.custom_dataset import TFDataset
from dal.data_utils import *

def weighted_cross_entropy_with_logits(logits, targets, pos_weight = 2.5):
    logits = torch.sigmoid(logits)
    logits = torch.clamp(logits, min= 1.e-6, max=1. - 1.e-6)
    loss = -targets * torch.log(logits) * pos_weight + (1 - targets) * - torch.log(1 - logits)
    if loss.isnan().any():
        raise Exception("nan in loss!")
    return loss.sum()

def learn(splits, indexes, vecs, params, output):

    num_nodes = params['d']; learning_rate = params['lr']; batch_size = params['b']; num_epochs = params['e']

    input_size = len(indexes['i2s'])
    output_size = len(indexes['i2c'])

    try: os.makedirs(output)
    except FileExistsError as ex:
        print("This model already exists")
        return

    train_valid_loss = dict()
    for i in range(len(splits['folds'].keys())):
        train_valid_loss[i] = {'train': [], 'valid': []}

    start_time = time.time()
    # Training K-fold
    for foldidx in splits['folds'].keys():
        # Retrieving the folds
        X_train = vecs['skill'][splits['folds'][foldidx]['train'], :]; y_train = vecs['member'][splits['folds'][foldidx]['train']]
        X_valid = vecs['skill'][splits['folds'][foldidx]['valid'], :]; y_valid = vecs['member'][splits['folds'][foldidx]['valid']]

        training_matrix = TFDataset(X_train, y_train)
        validation_matrix = TFDataset(X_valid, y_valid)

        training_dataloader = DataLoader(training_matrix, batch_size=batch_size, shuffle=True, num_workers=0)
        validation_dataloader = DataLoader(validation_matrix, batch_size=batch_size, shuffle=True, num_workers=0)
        data_loaders = {"train": training_dataloader, "valid": validation_dataloader}

        model = FNN(input_size=input_size, output_size=output_size, param=params).to(device)

        # Loss and optimizer
        # criterion = nn.MSELoss()
        # criterion = nn.MultiLabelMarginLoss()
        # criterion = nn.BCELoss()
        # criterion = nn.BCEWithLogitsLoss()
        # criterion = nn.MultiLabelSoftMarginLoss()

        optimizer = optim.Adam(model.parameters(), lr=learning_rate)
        scheduler = ReduceLROnPlateau(optimizer, factor=0.5, patience=3, verbose=True)
        # scheduler = StepLR(optimizer, step_size=3, gamma=0.9)

        train_loss_values = []; valid_loss_values = []
        fold_time = time.time()
        for epoch in range(num_epochs):
            train_running_loss = valid_running_loss = 0.0
            # Each epoch has a training and validation phase
            for phase in ['train', 'valid']:
                for batch_idx, (X, y) in enumerate(data_loaders[phase]):
                    torch.cuda.empty_cache()
                    X = X.float().to(device=device)# Get data to cuda if possible
                    y = y.float().to(device=device)
                    if phase == 'train':
                        model.train(True)  # scheduler.step()
                        # forward
                        optimizer.zero_grad()
                        y_ = model(X)
                        loss = weighted_cross_entropy_with_logits(y_, y)
                        # backward
                        loss.backward()
                        # clip_grad_value_(model.parameters(), 1)
                        optimizer.step()
                        train_running_loss += loss.item()
                    else:#valid
                        model.train(False)  # Set model to valid mode
                        y_ = model(X)
                        loss = weighted_cross_entropy_with_logits(y_, y)
                        valid_running_loss += loss.item()
                    # print(f'Fold {foldidx}/{len(splits["folds"]) - 1}, Epoch {epoch}/{num_epochs - 1}, Minibatch {batch_idx}/{int(X_train.shape[0] / batch_size)}, Phase {phase}'
                    #       f', Running Loss {phase} {loss.item()}'
                    #       # f", Time {time.time() - fold_time}, Overal {time.time() - start_time} "
                    #       )
                # Appending the loss of each epoch to plot later
                if phase == 'train': train_loss_values.append(train_running_loss/X_train.shape[0])
                else: valid_loss_values.append(valid_running_loss/X_valid.shape[0])
                print(f'Fold {foldidx}/{len(splits["folds"]) - 1}, Epoch {epoch}/{num_epochs - 1}'
                      f', Running Loss {phase} {train_loss_values[-1] if phase == "train" else valid_loss_values[-1]}'
                      # f", Time {time.time() - fold_time}, Overal {time.time() - start_time} "
                      )
            torch.save(model.state_dict(), f"{output}/state_dict_model.f{foldidx}.e{epoch}.pt")
            scheduler.step(valid_running_loss/X_valid.shape[0])

        model_path = f"{output}/state_dict_model_{foldidx}.pt"
        torch.save(model.state_dict(), model_path)
        train_valid_loss[foldidx]['train'] = train_loss_values
        train_valid_loss[foldidx]['valid'] = valid_loss_values

    plot_path = f"{output}/train_valid_loss.json"
    
    with open(plot_path, 'w') as outfile:
        json.dump(train_valid_loss, outfile)

def plot(plot_path, output):
    with open(plot_path) as infile:
        train_valid_loss = json.load(infile)
    for foldidx in train_valid_loss.keys():
        plt.plot(train_valid_loss[foldidx]['train'], label='Training Loss')
        plt.plot(train_valid_loss[foldidx]['valid'], label='Validation Loss')
        plt.legend(loc='upper right')
        plt.title(f'Training and Validation Loss for fold #{foldidx}')
        plt.savefig(f'{output}/fold{foldidx}.png', dpi=100, bbox_inches='tight')
        plt.show()  

def eval(model_path, splits, indexes, vecs, params):
    if not os.path.isdir(model_path):
        print("The model does not exist!")
        return
    input_size = len(indexes['i2s'])
    output_size = len(indexes['i2c'])
    
    auc_path = f"{model_path}/train_valid_auc.json"
    if os.path.isfile(auc_path):
        print(f"Reports can be found in: {model_path}")

    # Initialize auc dictionary for training and validation
    # Initialize roc dictionary for training and validation
    auc = dict()
    roc_train = dict()
    roc_valid = dict()
    with open(f"{model_path}/train_valid_loss.json", 'r') as infile:
        data = json.load(infile)
        for foldidx in splits['folds'].keys():
            if np.any(np.isnan(data[str(foldidx)]["train"])): continue
            auc[foldidx] = {'train': "", 'valid': ""}
            roc_train[foldidx] = {'fpr': [], 'tpr': []}
            roc_valid[foldidx] = {'fpr': [], 'tpr': []}


    # Load
    with open(f"{model_path}/train_valid_loss.json", 'r') as infile:
        data = json.load(infile)
        for foldidx in splits['folds'].keys():
            if np.any(np.isnan(data[str(foldidx)]["train"])): continue

            X_train = vecs['skill'][splits['folds'][foldidx]['train'], :]
            y_train = vecs['member'][splits['folds'][foldidx]['train']]
            X_valid = vecs['skill'][splits['folds'][foldidx]['valid'], :]
            y_valid = vecs['member'][splits['folds'][foldidx]['valid']]

            training_matrix = TFDataset(X_train, y_train)
            validation_matrix = TFDataset(X_valid, y_valid)

            training_dataloader = DataLoader(training_matrix, batch_size=params['b'], shuffle=True, num_workers=0)
            validation_dataloader = DataLoader(validation_matrix, batch_size=params['b'], shuffle=True, num_workers=0)

            model = FNN(input_size=input_size, output_size=output_size, param=params).to(device)
            model.load_state_dict(torch.load(f'{model_path}/state_dict_model_{foldidx}.pt'))
            model.eval()
            

            # Measure AUC for each fold and store in dict to later save as json
            auc_train = roc_auc(training_dataloader, model, device)
            auc_valid = roc_auc(validation_dataloader, model, device)
            auc[foldidx]['train'] = auc_train
            auc[foldidx]['valid'] = auc_valid

            roc_train[foldidx]['fpr'], roc_train[foldidx]['tpr'] = plot_roc(training_dataloader, model, device)
            roc_valid[foldidx]['fpr'], roc_valid[foldidx]['tpr'] = plot_roc(validation_dataloader, model, device)

            # Measure Precision, recall, and F1 and save to txt
            train_rep_path = f'{model_path}/train_rep_{foldidx}.txt'
            if not os.path.isfile(train_rep_path):
                cls = cls_rep(training_dataloader, model, device)
                with open(train_rep_path, 'w') as outfile:
                    outfile.write(cls)
            else:
                with open(train_rep_path, 'r') as infile:
                    cls = infile.read()
            
            print(f"Training report of fold{foldidx}:\n", cls)

            valid_rep_path = f'{model_path}/valid_rep_{foldidx}.txt'
            if not os.path.isfile(valid_rep_path):
                cls = cls_rep(validation_dataloader, model, device)
                with open(valid_rep_path, 'w') as outfile:
                    outfile.write(cls)
            else:
                with open(valid_rep_path, 'r') as infile:
                    cls = infile.read()

            print(f"Validation report of fold{foldidx}:\n", cls)

    colors = ['deeppink', 'royalblue', 'darkviolet', 'aqua', 'darkorange', 'maroon', 'chocolate']

    # Plot the roc curves for training set
    plt.figure()
    for foldidx in splits['folds'].keys():

        plt.plot(roc_train[foldidx]['fpr'], roc_train[foldidx]['tpr'],
                 label='micro-average ROC curve #{:.0f} of training set(auc = {:.2f})'.format(foldidx, float(auc[foldidx]["train"])),
                 color=colors[foldidx], linestyle=':', linewidth=4)

    plt.xlabel('False positive rate')
    plt.ylabel('True positive rate')
    plt.title('ROC curves for training set')
    plt.legend()
    plt.show()

    # Plot the roc curves for validation set
    plt.figure()
    for foldidx in splits['folds'].keys():
        plt.plot(roc_valid[foldidx]['fpr'], roc_valid[foldidx]['tpr'],
                 label='micro-average ROC curve #{:.0f} of validation set (auc = {:.2f})'.format(foldidx, float(auc[foldidx]["valid"])),
                 color=colors[foldidx], linestyle=':', linewidth=4)

    plt.xlabel('False positive rate')
    plt.ylabel('True positive rate')
    plt.title('ROC curves for validation set')
    plt.legend()
    plt.show()

    with open(auc_path, 'w') as outfile:
        json.dump(auc, outfile)

def test(model_path, splits, indexes, vecs, params):
    if not os.path.isdir(model_path):
        print("The model does not exist!")
        return

    auc_path = f"{model_path}/test_auc.json"

    if os.path.isfile(auc_path):
        print(f"Reports can be found in: {model_path}")
        # return

    input_size = len(indexes['i2s'])
    output_size = len(indexes['i2c'])

    # Load
    X_test = vecs['skill'][splits['test'], :]
    y_test = vecs['member'][splits['test']]

    test_matrix = TFDataset(X_test, y_test)

    test_dataloader = DataLoader(test_matrix, batch_size=params['b'], shuffle=True, num_workers=0)
    
    # Initialize auc dictionary
    auc = dict()
    fpr = dict()
    tpr = dict()
    with open(f"{model_path}/train_valid_loss.json", 'r') as infile:
        data = json.load(infile)
        for foldidx in splits['folds'].keys():
            if np.any(np.isnan(data[str(foldidx)]["train"])): continue
            auc[foldidx] = ""
            fpr[foldidx] = []
            tpr[foldidx] = []

    for foldidx in auc.keys():
        model = FNN(input_size=input_size, output_size=output_size, param=params).to(device)
        model.load_state_dict(torch.load(f'{model_path}/state_dict_model_{foldidx}.pt'))
        model.eval()

        # Measure AUC for each fold and store in dict to later save as json
        auc_test = roc_auc(test_dataloader, model, device)
        auc[foldidx] = auc_test
        fpr[foldidx], tpr[foldidx] = plot_roc(test_dataloader, model, device)

        # Measure Precision, recall, and F1 and save to txt
        test_rep_path = f'{model_path}/test_rep_{foldidx}.txt'
        if not os.path.isfile(test_rep_path):
            cls = cls_rep(test_dataloader, model, device)
            with open(test_rep_path, 'w') as outfile:
                outfile.write(cls)
        else:
            with open(test_rep_path, 'r') as infile:
                cls = infile.read()

        print(f"Test report of fold{foldidx}:\n", cls)

    auc_values = list(map(float, list(auc.values())))
    auc_mean = np.mean(auc_values)
    auc_var = np.var(auc_values)
    auc["mean"] = auc_mean
    auc["var"] = auc_var

    with open(auc_path, 'w') as outfile:
        json.dump(auc, outfile)

    colors = ['deeppink', 'royalblue', 'darkviolet', 'aqua', 'darkorange', 'maroon', 'chocolate']
    plt.figure()
    for foldidx in fpr.keys():
        plt.plot(fpr[foldidx], tpr[foldidx],
                 label='micro-average ROC curve #{:.0f} of test set (auc = {:.2f})'.format(foldidx, float(
                     auc[foldidx])),
                 color=colors[foldidx], linestyle=':', linewidth=4)

    plt.xlabel('False positive rate')
    plt.ylabel('True positive rate')
    plt.title('ROC curves for test set')
    plt.legend()
    plt.show()

def main(splits, vecs, indexes, output, settings, cmd):
    # Build a folder for this model for the first time
    output = f"{output}/" \
             f"t{vecs['skill'].shape[0]}." \
             f"s{vecs['skill'].shape[1]}." \
             f"e{vecs['member'].shape[1]}." \
             f"{'.'.join([k + str(v) for k,v in settings.items()])}"

    if 'train' in cmd: learn(splits, indexes, vecs, settings, output)

    if 'plot' in cmd:
        plot_path = f"{output}/train_valid_loss.json"
        plot(plot_path, output)

    if 'eval' in cmd: eval(output, splits, indexes, vecs, settings)

    if 'test' in cmd: test(output, splits, indexes, vecs, settings)