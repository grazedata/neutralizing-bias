"""
finetune both models jointly

python tagging_train.py --train ../../data/v5/final/bias --test ../../data/v5/final/bias --working_dir TEST/ --train_batch_size 32 --test_batch_size 16 
"""




from collections import defaultdict
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from tqdm import tqdm
import os
import torch
from pytorch_pretrained_bert.tokenization import BertTokenizer
from simplediff import diff
import pickle
from tensorboardX import SummaryWriter
import torch.optim as optim
import torch.nn as nn
import numpy as np
from collections import Counter
import math
import functools

from pytorch_pretrained_bert.modeling import BertEmbeddings
from pytorch_pretrained_bert.optimization import BertAdam

from seq2seq_data import get_dataloader
import seq2seq_model
import seq2seq_utils as utils

from joint_args import ARGS


import tagging_model
import tagging_utils


BERT_MODEL = "bert-base-uncased"

# TODO REFACTER AWAY ALL THIS JUNK

train_data_prefix = ARGS.train
test_data_prefix = ARGS.test

working_dir = ARGS.working_dir
if not os.path.exists(working_dir):
    os.makedirs(working_dir)


TRAIN_TEXT = train_data_prefix + '.train.pre'
TRAIN_TEXT_POST = train_data_prefix + '.train.post'

TEST_TEXT = test_data_prefix + '.test.pre'
TEST_TEXT_POST = test_data_prefix + '.test.post'

WORKING_DIR = working_dir


TRAIN_BATCH_SIZE = ARGS.train_batch_size
TEST_BATCH_SIZE = ARGS.test_batch_size


EPOCHS = ARGS.epochs

MAX_SEQ_LEN = ARGS.max_seq_len

CUDA = (torch.cuda.device_count() > 0)
                                                                


# # # # # # # # ## # # # ## # # DATA # # # # # # # # ## # # # ## # #
tokenizer = BertTokenizer.from_pretrained(BERT_MODEL, cache_dir=WORKING_DIR + '/cache')
tok2id = tokenizer.vocab
tok2id['<del>'] = len(tok2id)


if ARGS.pretrain_data: 
    pretrain_dataloader, num_pretrain_examples = get_dataloader(
        ARGS.pretrain_data, ARGS.pretrain_data, 
        tok2id, TRAIN_BATCH_SIZE, MAX_SEQ_LEN, WORKING_DIR + '/pretrain_data.pkl',
        noise=True,
        ARGS=ARGS)

train_dataloader, num_train_examples = get_dataloader(
    TRAIN_TEXT, TRAIN_TEXT_POST, 
    tok2id, TRAIN_BATCH_SIZE, MAX_SEQ_LEN, WORKING_DIR + '/train_data.pkl',
    add_del_tok=ARGS.add_del_tok, 
    tok_dist_path=ARGS.tok_dist_train_path,
    ARGS=ARGS)
eval_dataloader, num_eval_examples = get_dataloader(
    TEST_TEXT, TEST_TEXT_POST,
    tok2id, TEST_BATCH_SIZE, MAX_SEQ_LEN, WORKING_DIR + '/test_data.pkl',
    test=True, add_del_tok=ARGS.add_del_tok, 
    tok_dist_path=ARGS.tok_dist_test_path,
    ARGS=ARGS)



# # # # # # # # ## # # # ## # # MODELS # # # # # # # # ## # # # ## # #
if ARGS.no_tok_enrich:
    model = seq2seq_model.Seq2Seq(
        vocab_size=len(tok2id), hidden_size=ARGS.hidden_size,
        emb_dim=768, dropout=0.2, tok2id=tok2id)
else:
    model = seq2seq_model.Seq2SeqEnrich(
        vocab_size=len(tok2id), hidden_size=ARGS.hidden_size,
        emb_dim=768, dropout=0.2, tok2id=tok2id)
if CUDA:
    model = model.cuda()

model_parameters = filter(lambda p: p.requires_grad, model.parameters())
params = sum([np.prod(p.size()) for p in model_parameters])
print('NUM PARAMS: ', params)



# # # # # # # # ## # # # ## # # OPTIMIZER # # # # # # # # ## # # # ## # #
writer = SummaryWriter(WORKING_DIR)

if ARGS.bert_encoder:
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.0}
    ]

    num_train_steps = (num_train_examples * 40)
    if ARGS.pretrain_data: 
        num_train_steps += (num_pretrain_examples * ARGS.pretrain_epochs)

    optimizer = BertAdam(optimizer_grouped_parameters,
                         lr=5e-5,
                         warmup=0.1,
                         t_total=num_train_steps)

else:
    optimizer = optim.Adam(model.parameters(), lr=0.0003)


# # # # # # # # ## # # # ## # # LOSS # # # # # # # # ## # # # ## # #
# TODO -- REFACTOR THIS BIG TIME!

weight_mask = torch.ones(len(tok2id))
weight_mask[0] = 0
criterion = nn.CrossEntropyLoss(weight=weight_mask)
per_tok_criterion = nn.CrossEntropyLoss(weight=weight_mask, reduction='none')

if CUDA:
    weight_mask = weight_mask.cuda()
    criterion = criterion.cuda()
    per_tok_criterion = per_tok_criterion.cuda()

def cross_entropy_loss(logits, labels, apply_mask=None):
    return criterion(
        logits.contiguous().view(-1, len(tok2id)), 
        labels.contiguous().view(-1))


def weighted_cross_entropy_loss(logits, labels, apply_mask=None):
    # weight apply_mask = wehere to apply weight
    weights = apply_mask.contiguous().view(-1)
    weights = ((ARGS.debias_weight - 1) * weights) + 1.0

    per_tok_losses = per_tok_criterion(
        logits.contiguous().view(-1, len(tok2id)), 
        labels.contiguous().view(-1))

    per_tok_losses = per_tok_losses * weights

    loss = torch.mean(per_tok_losses[torch.nonzero(per_tok_losses)].squeeze())

    return loss

if ARGS.debias_weight == 1.0:
    loss_fn = cross_entropy_loss
else:
    loss_fn = weighted_cross_entropy_loss




##################################################################
##################################################################
##################################################################
#                       TAGGER
##################################################################
##################################################################
##################################################################

if ARGS.extra_features_top:
    tagging_model= tagging_model.BertForMultitaskWithFeaturesOnTop.from_pretrained(
            ARGS.bert_model,
            cls_num_labels=ARGS.num_categories,
            tok_num_labels=ARGS.num_tok_labels,
            cache_dir=ARGS.working_dir + '/cache',
            tok2id=tok2id,
            args=ARGS)
elif ARGS.extra_features_bottom:
    tagging_model= tagging_model.BertForMultitaskWithFeaturesOnBottom.from_pretrained(
            ARGS.bert_model,
            cls_num_labels=ARGS.num_categories,
            tok_num_labels=ARGS.num_tok_labels,
            cache_dir=ARGS.working_dir + '/cache',
            tok2id=tok2id,
            args=ARGS)
else:
    tagging_model= tagging_model.BertForMultitask.from_pretrained(
        ARGS.bert_model,
        cls_num_labels=ARGS.num_categories,
        tok_num_labels=ARGS.num_tok_labels,
        cache_dir=ARGS.working_dir + '/cache',
        tok2id=tok2id)
        
if os.path.exists(ARGS.checkpoint):
    print('LOADING FROM ' + ARGS.checkpoint)
    tagging_model.load_state_dict(torch.load(ARGS.checkpoint))
    print('...DONE')
               
if CUDA:
    tagging_model = tagging_model.cuda()

tagging_loss_fn = tagging_utils.build_loss_fn(ARGS)




##################################################################
##################################################################
##################################################################
#                       JOINT MODEL
##################################################################
##################################################################
##################################################################

class JointModel(nn.Module):
    def __init__(self, debias_model, tagging_model):
        super(JointModel, self).__init__()
    
        # TODO SHARING EMBEDDINGS FROM DEBIAS
        self.debias_model = debias_model
        self.tagging_model = tagging_model

        # TODO SM IN DIFFERENT DIRECTIONS
        self.bridge_sm = nn.Softmax(dim=2)


    # TODO -- EVAL, INFERENCE FORWARD
    def inference_forward_greedy(self,
            pre_id, post_in_id, pre_mask, pre_len, tok_dist, type_id, ignore_enrich=False,   # debias arggs
            rel_ids=None, pos_ids=None, categories=None):      # tagging args
        global CUDA
        """ argmax decoding """
        # Initialize target with <s> for every sentence
        tgt_input = Variable(torch.LongTensor([
                [post_start_id] for i in range(pre_id.size(0))
        ]))
        if CUDA:
            tgt_input = tgt_input.cuda()

        out_logits = []

        for i in range(max_len):
            # run input through the model
            with torch.no_grad():
                decoder_logit, word_probs = self.forward(
                    pre_id, tgt_input, pre_mask, pre_len, tok_dist, type_id,
                    rel_ids=rel_ids, pos_ids=pos_ids, categories=categories)
            next_preds = torch.max(word_probs[:, -1, :], dim=1)[1]
            tgt_input = torch.cat((tgt_input, next_preds.unsqueeze(1)), dim=1)

        # [batch, len ] predicted indices
        return tgt_input.detach().cpu().numpy()
                
    def forward(self, 
        pre_id, post_in_id, pre_mask, pre_len, tok_dist, type_id, ignore_enrich=False,   # debias arggs
        rel_ids=None, pos_ids=None, categories=None):      # tagging args

        # TODO IGNORE THIS IF NOT ALL PARAMS ARE PROVIDED
        if rel_ids is None or pos_ids is None:
            is_bias_probs = tok_dist
        else:
            category_logits, tok_logits = self.tagging_model(
                pre_id, attention_mask=1.0-pre_mask, rel_ids=rel_ids, pos_ids=pos_ids, categories=categories)

            # TODO VARIOUS BRIDGES (SAME AS OTHER)
            tok_probs = self.bridge_sm(tok_logits[:, :, :2])
            is_bias_probs = tok_probs[:, :, -1]

        post_logits, post_probs = self.debias_model(
            pre_id, post_in_id, pre_mask, pre_len,  is_bias_probs, type_id)

        return post_logits, post_probs





joint_model = JointModel(debias_model=model, tagging_model=tagging_model)










# # # # # # # # # # # PRETRAINING (optional) # # # # # # # # # # # # # # # #
if ARGS.pretrain_data:
    print('PRETRAINING...')
    for epoch in range(ARGS.pretrain_epochs):
        model.train()
        losses = utils.train_for_epoch(model, pretrain_dataloader, tok2id, optimizer, cross_entropy_loss, 
            ignore_enrich=ARGS.ignore_pretrain_enrich)
        writer.add_scalar('pretrain/loss', np.mean(losses), epoch)



# # # # # # # # # # # # TRAINING # # # # # # # # # # # # # #
print('INITIAL EVAL...')
# joint_model.eval()
# hits, preds, golds, srcs = utils.run_eval(
#     joint_model, eval_dataloader, tok2id, WORKING_DIR + '/results_initial.txt',
#     MAX_SEQ_LEN, ARGS.beam_width)
# # writer.add_scalar('eval/partial_bleu', utils.get_partial_bleu(srcs, golds, srcs), epoch+1)
# writer.add_scalar('eval/bleu', utils.get_bleu(preds, golds), 0)
# writer.add_scalar('eval/true_hits', np.mean(hits), 0)

for epoch in range(EPOCHS):
    print('EPOCH ', epoch)
    print('TRAIN...')
    joint_model.train()
    for step, batch in enumerate(tqdm(train_dataloader)):
        if CUDA: 
            batch = tuple(x.cuda() for x in batch)
        (
            pre_id, pre_mask, pre_len, 
            post_in_id, post_out_id, 
            pre_tok_label_id, post_tok_label_id, tok_dist,
            replace_id, rel_ids, pos_ids, type_ids, categories
        ) = batch      
        post_logits, post_probs = joint_model(
            pre_id, post_in_id, pre_mask, pre_len, tok_dist, type_ids,
            rel_ids=rel_ids, pos_ids=pos_ids, categories=categories)
        loss = loss_fn(post_logits, post_out_id, post_tok_label_id)
        loss.backward()
        norm = nn.utils.clip_grad_norm_(joint_model.parameters(), 3.0)
        optimizer.step()
        joint_model.zero_grad()
        
    
    losses = utils.train_for_epoch(joint_model, train_dataloader, tok2id, optimizer, loss_fn)
    writer.add_scalar('train/loss', np.mean(losses), epoch+1)
    
    print('SAVING...')
    joint_model.save(WORKING_DIR + '/model_%d.ckpt' % (epoch+1))

    print('EVAL...')
    joint_model.eval()
    hits, preds, golds, srcs = utils.run_eval(
        joint_model, eval_dataloader, tok2id, WORKING_DIR + '/results_%d.txt' % epoch,
        MAX_SEQ_LEN, ARGS.beam_width)
    # writer.add_scalar('eval/partial_bleu', utils.get_partial_bleu(preds, golds, srcs), epoch+1)
    writer.add_scalar('eval/bleu', utils.get_bleu(preds, golds), epoch+1)
    writer.add_scalar('eval/true_hits', np.mean(hits), epoch+1)






















