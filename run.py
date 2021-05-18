from tqdm import tqdm, trange
import logging
import argparse
import os
import pickle
import random
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from sklearn.metrics import f1_score, precision_recall_fscore_support, classification_report

from transformers import BertConfig, BertTokenizerFast, AdamW
from transformers.optimization import get_linear_schedule_with_warmup

from load_data import load_and_cache_dataset, load_and_cache_predict_dataset, NewsDataset
from bert_crf import BertCRFForTokenClassification, \
    BertCRFForJointTokenAndSequenceClassification, \
    BertForSequenceClassification, \
    BertForTokenClassification, \
    BertForJointTokenAndSequenceClassification, \
    BertForStackedTokenAndSequenceClassification

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)


def set_seed(seed=24):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def get_tag_correct(pred, label, args):
    tags = set(list(pred))
    if 11 in tags:
        tags.remove(11)

    true_tags = set(label)
    if not args.do_predict:
        if 11 in true_tags:
            true_tags.remove(11)
        if -100 in true_tags:
            true_tags.remove(-100)

    if len(tags) > 0 and tags.issubset(true_tags):
        return 1
    elif len(tags) == 0 and len(true_tags) == 0:
        return 1
    else:
        return 0


def evaluate(test_dataset, model, args):
    logger.info('Start Evaluating')
    test_sampler = SequentialSampler(test_dataset)
    test_dataloader = DataLoader(test_dataset, batch_size=args.per_gpu_batch_size * args.n_gpu, sampler=test_sampler)

    if not isinstance(model, torch.nn.DataParallel):
        model = torch.nn.DataParallel(model)

    model.eval()
    test_iterator = tqdm(test_dataloader, desc="Iteration")
    tag_correct = 0
    seq_correct = 0
    ner_correct = 0
    ner_total = 0
    if args.TASK == 'stack':
        all_seq_preds = torch.zeros([1, 11])
    elif args.CRF:
        all_seq_preds = torch.zeros([1, 14])
    else:
        all_seq_preds = torch.zeros([1, 12])
    all_ner_preds = torch.zeros([0])
    all_ner_labels = torch.zeros([0])

    softmax = nn.Softmax(dim=2)

    with torch.no_grad():
        for batch in test_iterator:
            input_ids = batch['input_ids'].to(args.device)
            attention_mask = batch['attention_mask'].to(args.device)

            if args.TASK == 'seq':
                # labels = batch['seq_labels'].to(args.device)
                # outputs = model(input_ids, attention_mask=attention_mask)
                # pred = outputs[0].argmax(dim=1, keepdim=True)
                # seq_correct += pred.eq(labels.view_as(pred)).sum().item()
                outputs = model(input_ids, attention_mask=attention_mask)

                # for seq
                seq_labels = batch['seq_labels'].to(args.device)
                seq_pred = outputs[0].argmax(dim=1, keepdim=True)
                for pred, label in zip(seq_pred, seq_labels):
                    label = label.cpu().numpy()
                    pred = pred.cpu().item()
                    label = np.where(label == 1)[0]
                    if pred in label:
                        seq_correct += 1

                seq_pred = outputs[0]
                all_seq_preds = torch.cat([all_seq_preds, seq_pred.cpu().type_as(all_seq_preds)], dim=0)


            elif args.TASK == 'ner':
                outputs = model(input_ids, attention_mask=attention_mask)
                ner_labels = batch['ner_labels'].to(args.device)

                if args.CRF:
                    ner_pred = outputs[-1]
                else:
                    ner_pred = outputs[0].argmax(dim=2)

                    # assert len(torch.where(ner_pred==-100)[0]) == 0
                # print(torch.where(ner_pred==-100)[0])

                ner_values = outputs[0]
                ner_values = softmax(ner_values)
                ner_values = ner_values.max(dim=2)[0]
                ner_pred[ner_values < args.threshold] = 11

                ner_labels = ner_labels.view_as(ner_pred)
                ner_correct += ner_pred.eq(ner_labels)[ner_labels != -100].sum().item()
                ner_total += len(ner_labels[ner_labels != -100])

                ner_pred = ner_pred.cpu()
                ner_labels = ner_labels.view_as(ner_pred).cpu()
                # classification accuracy based on tags
                for pre, lab in zip(ner_pred.numpy(), ner_labels.numpy()):
                    tag_correct += get_tag_correct(pre, lab, args)

                # save results
                ner_pred = ner_pred.view([-1])
                ner_labels = ner_labels.view([-1])

                all_ner_preds = torch.cat([all_ner_preds, ner_pred.type_as(all_ner_preds)])
                all_ner_labels = torch.cat([all_ner_labels, ner_labels.type_as(all_ner_labels)])


            elif args.TASK in ['both', 'stack']:
                outputs = model(input_ids, attention_mask=attention_mask)

                # for seq
                seq_labels = batch['seq_labels'].to(args.device)[:, :-1] if \
                            (args.TASK == 'stack' and not args.do_predict) else batch['seq_labels'].to(args.device)

                # if args.CRF:
                #     seq_labels = torch.cat([seq_labels, torch.zeros([len(seq_labels), 2]).type_as(seq_labels).to(args.device)], dim=1)
                seq_pred = outputs[1].cpu().numpy()
                # print(seq_pred)
                for pred, label in zip(seq_pred, seq_labels):
                    seq_tags = set(list(np.where(pred > 0)[0]))
                    label = label.cpu().numpy()
                    label = set(list(np.where(label == 1)[0]))
                    if seq_tags == label:
                        seq_correct += 1

                seq_pred = outputs[1]
                all_seq_preds = torch.cat([all_seq_preds, seq_pred.cpu().type_as(all_seq_preds)], dim=0)

                # for ner
                ner_labels = batch['ner_labels'].to(args.device)

                if args.CRF:
                    ner_pred = outputs[-1]
                else:
                    ner_pred = outputs[0].argmax(dim=2)

                ner_values = outputs[0]
                ner_values = softmax(ner_values)
                ner_values = ner_values.max(dim=2)[0]
                ner_pred[ner_values < args.threshold] = 11

                ner_labels = ner_labels.view_as(ner_pred)
                ner_correct += ner_pred.eq(ner_labels)[ner_labels != -100].sum().item()
                ner_total += len(ner_labels[ner_labels != -100])

                ner_pred = ner_pred.cpu()
                ner_labels = ner_labels.view_as(ner_pred).cpu()
                # classification accuracy based on tags
                for pre, lab in zip(ner_pred.numpy(), ner_labels.numpy()):
                    tag_correct += get_tag_correct(pre, lab, args)

                # save results
                ner_pred = ner_pred.view([-1])
                ner_labels = ner_labels.view([-1])

                all_ner_preds = torch.cat([all_ner_preds, ner_pred.type_as(all_ner_preds)])
                all_ner_labels = torch.cat([all_ner_labels, ner_labels.type_as(all_ner_labels)])

    source_path = 'data/' + args.TASK + '_results' if args.predict_dir == '' else args.predict_dir
    if not os.path.exists(source_path):
        os.makedirs(source_path)

    logger.info('Saving predicted results to: ' + source_path)
    if args.TASK == 'seq':
        np.save(os.path.join(source_path, 'seq_pred.npy'), all_seq_preds)
    elif args.TASK == 'ner':
        np.save(os.path.join(source_path, 'ner_pred.npy'), all_ner_preds)
    elif args.TASK in ['both', 'stack']:
        np.save(os.path.join(source_path, 'ner_pred.npy'), all_ner_preds)
        np.save(os.path.join(source_path, 'seq_pred.npy'), all_seq_preds)

    if not args.do_predict:
        logger.info('\n')
        if args.TASK == 'seq':
            logger.info('Seq Accuracy: {}'.format(100. * seq_correct / len(test_dataloader.dataset)))
        elif args.TASK in ['ner', 'both', 'stack']:
            ner_report = classification_report(all_ner_labels[all_ner_labels != -100].numpy(),
                                            all_ner_preds[all_ner_labels != -100].numpy())
            logger.info(ner_report)
            micro_precision, micro_recall, micro_f1, _ = precision_recall_fscore_support(
                all_ner_labels[all_ner_labels != -100].numpy(), all_ner_preds[all_ner_labels != -100].numpy(),
                average='micro')
            macro_precision, macro_recall, macro_f1, _ = precision_recall_fscore_support(
                all_ner_labels[all_ner_labels != -100].numpy(), all_ner_preds[all_ner_labels != -100].numpy(),
                average='macro')
            weighted_precision, weighted_recall, weighted_f1, _ = precision_recall_fscore_support(
                all_ner_labels[all_ner_labels != -100].numpy(), all_ner_preds[all_ner_labels != -100].numpy(),
                average='weighted')
            logger.info('NER Accuracy: {}'.format(100. * ner_correct / ner_total))
            logger.info(
                'Micro Precision Recall F1: {} {} {}'.format(100. * micro_precision, 100. * micro_recall, 100. * micro_f1))
            logger.info(
                'Macro Precision Recall F1: {} {} {}'.format(100. * macro_precision, 100. * macro_recall, 100. * macro_f1))
            logger.info('Weighted Precision Recall F1: {} {} {}'.format(100. * weighted_precision, 100. * weighted_recall,
                                                                        100. * weighted_f1))

            # tag accuracy
            logger.info('Tag Accuracy: {}'.format(100. * tag_correct / len(test_dataloader.dataset)))

            if args.TASK in ['both', 'stack']:
                logger.info('Seq Accuracy: {}'.format(100. * seq_correct / len(test_dataloader.dataset)))

            if args.CRF:
                # print(model.module.crf.transitions)
                print(model.module.crf.ratio)
                np.save('data/crf.npy', model.module.crf.transitions.cpu().detach().numpy())


def predict(args):
    logger.info('Performing prediction')

    cache_path = 'data/cached_predict_{}_{}'.format(args.data_dir.split('/')[-1].replace(".","_"), args.max_seq_length)
    if not os.path.exists(cache_path):
        logger.info('Processing and cacheing data')
        load_and_cache_predict_dataset(args.data_dir, args.output_dir, args.max_seq_length)

    logger.info('Loading data from: ' + cache_path)
    with open(cache_path, 'rb') as f:
        dataset = pickle.load(f)
    predict_dataset = NewsDataset(dataset[0], dataset[1], dataset[2])

    model_path = args.output_dir
    logger.info('Loading model from: ' + str(model_path))

    MODEL_CLASS = get_model_class(args)
    config = BertConfig.from_pretrained(model_path)
    config.num_labels = 12
    model = MODEL_CLASS.from_pretrained(model_path, config=config)
    model.to(args.device)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    evaluate(predict_dataset, model, args)


def get_model_class(args):
    if args.TASK == 'seq':
        return BertForSequenceClassification
    elif args.TASK == 'ner':
        if args.CRF:
            return BertCRFForTokenClassification
        else:
            return BertForTokenClassification
    elif args.TASK == 'both':
        if args.CRF:
            return BertCRFForJointTokenAndSequenceClassification
        else:
            return BertForJointTokenAndSequenceClassification
    elif args.TASK == 'stack':
        return BertForStackedTokenAndSequenceClassification
    else:
        raise ValueError()

def main():
    # config
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--data_dir",
        default='data/ner',
        type=str,
        help="The input data dir. Should contain the files for the task.",
    )
    parser.add_argument(
        "--model_type",
        default='bert-large-cased-whole-word-masking',
        type=str,
        help="Model type",
    )
    parser.add_argument(
        "--CRF", action="store_true", help="Whether use CRF or not"
    )
    parser.add_argument(
        "--do_predict", action="store_true", help="Add the argument during backtesting on news"
    )
    parser.add_argument(
        "--TASK",
        default=None,
        type=str,
        required=True,
        help="choose from ['seq', 'ner', 'both']",
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--predict_dir",
        default='',
        type=str,
        help="The directory to save predict result. Use it with --do_predict",
    )
    parser.add_argument(
        "--max_seq_length", default=256, type=int, help="Max sequence length for prediction"
    )
    parser.add_argument(
        "--bert_lr", default=5e-5, type=float, help="The peak learning rate for BERT."
    )
    parser.add_argument(
        "--crf_transition_lr", default=1e-4, type=float, help="The peak learning rate for CRF transition matrix."
    )
    parser.add_argument(
        "--crf_ratio_lr", default=1e-4, type=float, help="The peak learning rate for CRF ratio."
    )
    parser.add_argument(
        "--threshold", default=0, type=float, help="The threshold for NER."
    )
    parser.add_argument(
        "--epoch", default=3, type=int, help="Number of epoch for training"
    )
    parser.add_argument(
        "--per_gpu_batch_size", default=2, type=int, help="Batch size"
    )
    parser.add_argument(
        "--gradient_accumulation_steps", default=2, type=int, help="Batch size"
    )
    parser.add_argument(
        "--seed", default=24, type=int, help="Random seed"
    )
    parser.add_argument(
        "--n_gpu", default=4, type=int, help="Number of GPUs"
    )
    parser.add_argument(
        "--device", default='cpu', type=str, help="Number of GPUs"
    )

    args = parser.parse_args()
    args.n_gpu = torch.cuda.device_count()
    args.device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

    # initialize
    set_seed(args.seed)
    MODEL_CLASS = get_model_class(args)

    # handle predict
    if args.do_predict:
        predict(args)
        return

    # load data
    logger.info('Processing and loading data')
    cache_path = 'data/cached_train_test_{}'.format(args.max_seq_length)
    if not os.path.exists(cache_path):
        load_and_cache_dataset(args.data_dir, args.model_type, args.max_seq_length)

    with open(cache_path, 'rb') as f:
        dataset = pickle.load(f)
        train_dataset = NewsDataset(dataset[0], dataset[1], dataset[2])
        test_dataset = NewsDataset(dataset[3], dataset[4], dataset[5])

    logger.info(
        'Total training batch size: {}'.format(args.per_gpu_batch_size * args.gradient_accumulation_steps * args.n_gpu))

    config = BertConfig.from_pretrained(args.model_type)
    config.num_labels = 12
    model = MODEL_CLASS.from_pretrained(args.model_type, config=config)

    crf_transitions = ['crf.transitions']
    crf_ratio = ['crf.ratio']
    crf_transitions_list = list(filter(lambda kv: kv[0] in crf_transitions, model.named_parameters()))
    crf_ratio_list = list(filter(lambda kv: kv[0] in crf_ratio, model.named_parameters()))
    bert_list = list(
        filter(lambda kv: kv[0] not in crf_ratio and kv[0] not in crf_transitions, model.named_parameters()))

    crf_transitions_params = []
    crf_ratio_params = []
    bert_params = []
    for params in crf_transitions_list:
        crf_transitions_params.append(params[1])
    for params in crf_ratio_list:
        crf_ratio_params.append(params[1])
    for params in bert_list:
        bert_params.append(params[1])

    optim = AdamW([{'params': crf_transitions_params, 'lr': args.crf_transition_lr},
                   {'params': crf_ratio_params, 'lr': args.crf_ratio_lr},
                   {'params': bert_params}], lr=args.bert_lr)
    total_steps = int(
        len(train_dataset) * args.epoch / (args.per_gpu_batch_size * args.gradient_accumulation_steps * args.n_gpu))
    scheduler = get_linear_schedule_with_warmup(optim, num_warmup_steps=int(total_steps * 0.1),
                                                num_training_steps=total_steps)
    # optim = AdamW(model.parameters(), lr=lr)

    # training
    logger.info('Start Training')
    logger.info(args)
    logger.info('Total Optimization Step: ' + str(total_steps))
    train_sampler = RandomSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, batch_size=args.per_gpu_batch_size * args.n_gpu, sampler=train_sampler,
                                  num_workers=4, worker_init_fn=worker_init_fn)

    model.to(args.device)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)
    model.train()
    model.zero_grad()

    epochs_trained = 0
    train_iterator = trange(epochs_trained, args.epoch, desc="Epoch")

    set_seed(args.seed)  # add here for reproducibility

    for _ in train_iterator:
        model.train()
        epoch_iterator = tqdm(train_dataloader, desc="Iteration")
        for step, batch in enumerate(epoch_iterator):
            optim.zero_grad()
            input_ids = batch['input_ids'].to(args.device)
            attention_mask = batch['attention_mask'].to(args.device)

            if args.TASK == 'seq':
                labels = batch['seq_labels'].to(args.device)
                outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
            elif args.TASK == 'ner':
                labels = batch['ner_labels'].to(args.device)
                outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
            elif args.TASK in ['both', 'stack']:
                seq_labels = batch['seq_labels'].to(args.device)
                ner_labels = batch['ner_labels'].to(args.device)
                if args.CRF:
                    seq_labels = torch.cat(
                        [seq_labels, torch.zeros([len(seq_labels), 2]).type_as(seq_labels).to(args.device)], dim=1)
                outputs = model(input_ids, attention_mask=attention_mask, seq_labels=seq_labels, ner_labels=ner_labels)

            loss = outputs[0].mean()
            loss.backward()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                optim.step()
                scheduler.step()

        # evaluation
        evaluate(test_dataset, model, args)

    # save model
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    logger.info("Saving model checkpoint to %s", args.output_dir)
    model_to_save = (
        model.module if hasattr(model, "module") else model
    )
    model_to_save.save_pretrained(args.output_dir)
    tokenizer = BertTokenizerFast.from_pretrained(args.model_type)
    tokenizer.save_pretrained(args.output_dir)
    torch.save(args, os.path.join(args.output_dir, "training_args.bin"))

    # Load a trained model and vocabulary that you have fine-tuned
    # model = model_class.from_pretrained(args.output_dir)
    # tokenizer = tokenizer_class.from_pretrained(args.output_dir)
    # model.to(args.device)


if __name__ == "__main__":
    main()