"""
Train a model for PM
"""

import os
from datetime import datetime
import time
import numpy as np
import random
import argparse
from shutil import copyfile
import torch

from transformers import BertTokenizer
import json
from tqdm import tqdm

parser = argparse.ArgumentParser()
parser.add_argument('--data_dir', type=str, default='dataset/WebNLG-P/data')  # {opt:[ WebNLG-P , NYT-P] }
parser.add_argument('--tokens_emb_dim', type=int, default=768, help='bert tokens embedding dimension.')
parser.add_argument('--word_emb_dim', type=int, default=300, help='Word embedding dimension.')
parser.add_argument('--position_emb_dim', type=int, default=20, help='Position embedding dimension.')
parser.add_argument('--dropout', type=float, default=0.4, help='Input and RNN dropout rate.')
parser.add_argument('--topn', type=int, default=1e10, help='Only finetune top N embeddings.')
parser.add_argument('--lr', type=float, default=2e-5)
parser.add_argument('--lr_decay', type=float, default=0)
parser.add_argument('--weight_decay', type=float, default=0, help='Applies to SGD and Adagrad.')
parser.add_argument('--optim', type=str, default='adam', help='sgd, adam or adamax.')
parser.add_argument('--num_epoch', type=int, default=100) # {nyt 60}
parser.add_argument('--load_saved', type=str, default='')
parser.add_argument('--batch_size', type=int, default=8)
parser.add_argument('--max_grad_norm', type=float, default=5.0, help='Gradient clipping.')
parser.add_argument('--log_step', type=int, default=400, help='Print log every k steps.')
parser.add_argument('--log', type=str, default='logs.txt', help='Write training log to file.')
parser.add_argument('--save_epoch', type=int, default=20, help='Save model checkpoints every k epochs.')
parser.add_argument('--save_dir', type=str, default='./saved_models', help='Root dir for saving models.')
parser.add_argument('--id', type=str, default='WebNLG-P-01', help='Model ID under which to save models.')
parser.add_argument('--info', type=str, default='', help='Optional info for the experiment.')

parser.add_argument('--seed', type=int, default=35)
parser.add_argument('--cuda', type=bool, default=torch.cuda.is_available())
parser.add_argument('--cpu', action='store_true', help='Ignore CUDA.')

## class num  WebNLG-P {214 2 171} NYT-P {37 2 24}
parser.add_argument('--classemb_num', type=int, default=214, help='classname embedding num.')
parser.add_argument('--entityclass_num', type=int, default=2, help='classname embedding num.')
parser.add_argument('--relationclass_num', type=int, default=171, help='classname embedding num.')

args = parser.parse_args()

torch.manual_seed(args.seed)
np.random.seed(args.seed)
torch.cuda.manual_seed(args.seed)
random.seed(args.seed)

from utils.loader import DataLoader
from models.remodel import REModel
from utils import helper, score, classinfo

opt = vars(args)

# load data
train_data = [json.loads(l) for l in tqdm(open(opt['data_dir'] + '/train.json'))]
dev_data = [json.loads(l) for l in tqdm(open(opt['data_dir'] + '/dev.json'))]
_, _, id2subj_type, subj_type2id, id2obj_type, obj_type2id = json.load(open(opt['data_dir'] + '/schemas.json', errors='ignore'))
id2predicate, predicate2id = json.load(open(opt['data_dir'] + '/rel2id.json'))  # relations file
id2predicate = {int(i): j for i, j in id2predicate.items()}

# class info
# entityclass_path = opt['data_dir'] + '/entityclass_name.txt'
# relationclass_path = opt['data_dir'] + '/relationclass_name.txt'
# classembedding_path = opt['data_dir'] + '/classname_embedding.txt'
#
# opt['entityclass_name'] = classinfo.getclassname(entityclass_path)
# opt['relationclass_name'] = classinfo.getclassname(relationclass_path)
# class_emb_matrix, classname2id = classinfo.get_class_embedding(classembedding_path, opt['classemb_num'],
#                                                                opt['word_emb_dim'])
# W_entityclass_emb = classinfo.load_class_embedding(classname2id, class_emb_matrix, opt['entityclass_name'])
# W_relationclass_emb = classinfo.load_class_embedding(classname2id, class_emb_matrix, opt['relationclass_name'])

opt['num_class'] = len(id2predicate)
opt['num_subj_type'] = opt['entityclass_num']-1  #len(id2subj_type)
opt['num_obj_type'] = opt['entityclass_num']-1  # len(id2obj_type)

model_name = 'bert-base-cased'
tokenizer = BertTokenizer.from_pretrained(model_name)

# load data
print("Loading data from {} with batch size {}...".format(opt['data_dir'], opt['batch_size']))
train_batch = DataLoader(tokenizer, train_data, predicate2id, subj_type2id, obj_type2id, opt['batch_size'], False, 1)

model_id = opt['id'] if len(opt['id']) > 1 else '0' + opt['id']
model_save_dir = opt['save_dir'] + '/' + model_id
opt['model_save_dir'] = model_save_dir
helper.ensure_dir(model_save_dir, verbose=True)

# save config
helper.save_config(opt, model_save_dir + '/config.json', verbose=True)
file_logger = helper.FileLogger(model_save_dir + '/' + opt['log'], header="# epoch\ttrain_loss\dev_p\tdev_r\tdev_f1")
# print model info
helper.print_config(opt)
print(opt['num_class'])
# model
model = REModel(opt, W_entityclass_emb=None, W_relationclass_emb=None)  #None or W_relationclass_emb
if opt['load_saved'] != '':
    model.load(opt['save_dir'] + '/' + opt['load_saved'] + '/best_model.pt')
dev_f1_history = []
current_lr = opt['lr']

global_step = 0
global_start_time = time.time()
format_str = '{}: step {}/{} (epoch {}/{}), loss = {:.6f} ({:.3f} sec/batch), lr: {:.6f}'
max_steps = len(train_batch) * opt['num_epoch']

# start training
for epoch in range(1, opt['num_epoch'] + 1):
    train_loss = 0
    for i, batch in enumerate(train_batch):
        start_time = time.time()
        global_step += 1
        loss = model.update(batch)
        train_loss += loss
        if global_step % opt['log_step'] == 0:
            duration = time.time() - start_time
            print(format_str.format(datetime.now(), global_step, max_steps, epoch, \
                                    opt['num_epoch'], loss, duration, current_lr))

    # eval on dev
    print("Evaluating on dev set...")
    dev_f1, dev_p, dev_r, results = score.other_evaluate(tokenizer, dev_data, id2predicate, model)

    train_loss = train_loss / train_batch.num_examples * opt['batch_size']
    best_f1 = dev_f1 if epoch == 1 or dev_f1 > max(dev_f1_history) else max(dev_f1_history)
    print(
        "epoch {}: train_loss = {:.6f}, dev_p = {:.6f}, dev_r = {:.6f}, dev_f1 = {:.4f}, best_f1 = {:.4f}".format(epoch, \
                                                                                                                  train_loss,
                                                                                                                  dev_p,
                                                                                                                  dev_r,
                                                                                                                  dev_f1,
                                                                                                                  best_f1))
    file_logger.log("{}\t{:.6f}\t{:.6f}\t{:.4f}\t{:.4f}".format(epoch, train_loss, dev_p, dev_r, dev_f1))

    # save
    model_file = model_save_dir + '/checkpoint_epoch_{}.pt'.format(epoch)
    model.save(model_file, epoch)
    if epoch == 1 or dev_f1 >= max(dev_f1_history):
        copyfile(model_file, model_save_dir + '/best_model.pt')
        print("new best model saved.")
        with open(model_save_dir + '/best_dev_results.json', 'w') as fw:
            json.dump(results, fw, indent=4, ensure_ascii=False)
        print("new best results saved.")
    if epoch % opt['save_epoch'] != 0:
        os.remove(model_file)

    # lr schedule
    if len(dev_f1_history) > 10 and dev_f1 <= dev_f1_history[-1] and \
            opt['optim'] in ['sgd', 'adagrad']:
        current_lr *= opt['lr_decay']
        model.update_lr(current_lr)

    dev_f1_history += [dev_f1]
    print("")

print("Training ended with {} epochs.".format(epoch))

