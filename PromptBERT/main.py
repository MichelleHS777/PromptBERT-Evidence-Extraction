import torch
import numpy as np
from tqdm import tqdm
from model import PromptBERT
from config import set_args
from transformers.models.bert import BertTokenizer
from torch.utils.data import DataLoader
from transformers import AdamW, get_linear_schedule_with_warmup
from data_helper import load_data, SentDataSet, collate_func, convert_token_id
import json

device = torch.device('cuda')
args = set_args()

# read file
dataset = open(args.test_data_path, 'r', encoding='utf-8')

# save file
save = open(args.save_file, 'a+', encoding='utf-8')
save_out5 = open(args.save_file_out5, 'a+', encoding='utf-8')

def get_similar_sentence():
    model.load_state_dict(torch.load(f"./checkpoint/train_claim_sent.ckpt"))
    model.eval()
    for data in tqdm(dataset, desc='getting similar sentence...'):
        data = eval(data)
        claimId = data['claimId']
        claim = data['claim']
        evidences = data['evidences']
        label = data['label']
        # save similarity score
        sent2sim = {}
        for ev_sent in evidences:
            if ev_sent in sent2sim:
                continue
            sent2sim[ev_sent] = cosSimilarity(claim, ev_sent, model, tokenizer)
        sent2sim = list(sent2sim.items())
        # sort the similarity score
        sent2sim.sort(key=lambda s: s[1], reverse=True)
        # get evidence sentence threshold > 0.8
        ev_sent = [s[0] for s in sent2sim if s[1] > 0.8]
        print('ev_sent:', ev_sent)
        data = json.dumps({'claimId': claimId, 'claim': claim, 'evidences': ev_sent, 'label': label},
                          ensure_ascii=False)
        save.write(data + "\n")

        # # get evidence sentence output 5
        # ev_sent_out5 = [s[0] for s in sent2sim[:5]]
        # print('ev_sent out5:', ev_sent_out5)
        # data = json.dumps({'claimId': claimId, 'claim': claim, 'evidences': ev_sent_out5, 'label': label},
        #                   ensure_ascii=False)
        # save_out5.write(data + "\n")

    save.close()
    # save_out5.close()

def cosSimilarity(sent1, sent2, model, tokenizer):
    model.eval()
    s1_input_id, s1_mask, s1_segment_id, s1_t_input_id, s1_t_mask, s1_t_segment_id = convert_token_id(sent1,
                                                                                                      tokenizer)
    s2_input_id, s2_mask, s2_segment_id, s2_t_input_id, s2_t_mask, s2_t_segment_id = convert_token_id(sent2,
                                                                                                      tokenizer)
    if torch.cuda.is_available():
        s1_input_id, s1_mask, s1_segment_id, s1_t_input_id, s1_t_mask, s1_t_segment_id = s1_input_id.cuda(), s1_mask.cuda(), s1_segment_id.cuda(), s1_t_input_id.cuda(), s1_t_mask.cuda(), s1_t_segment_id.cuda()
        s2_input_id, s2_mask, s2_segment_id, s2_t_input_id, s2_t_mask, s2_t_segment_id = s2_input_id.cuda(), s2_mask.cuda(), s2_segment_id.cuda(), s2_t_input_id.cuda(), s2_t_mask.cuda(), s2_t_segment_id.cuda()
    else:
        s1_input_id, s1_mask, s1_segment_id, s1_t_input_id, s1_t_mask, s1_t_segment_id = s1_input_id.cpu(), s1_mask.cpu(), s1_segment_id.cpu(), s1_t_input_id.cpu(), s1_t_mask.cpu(), s1_t_segment_id.cpu()
        s2_input_id, s2_mask, s2_segment_id, s2_t_input_id, s2_t_mask, s2_t_segment_id = s2_input_id.cpu(), s2_mask.cpu(), s2_segment_id.cpu(), s2_t_input_id.cpu(), s2_t_mask.cpu(), s2_t_segment_id.cpu()

    with torch.no_grad():
        s1_embedding = model(prompt_input_ids=s1_input_id,
                             prompt_attention_mask=s1_mask,
                             prompt_token_type_ids=s1_segment_id,
                             template_input_ids=s1_t_input_id,
                             template_attention_mask=s1_t_mask,
                             template_token_type_ids=s1_t_segment_id)

        s2_embedding = model(prompt_input_ids=s2_input_id,
                             prompt_attention_mask=s2_mask,
                             prompt_token_type_ids=s2_segment_id,
                             template_input_ids=s2_t_input_id,
                             template_attention_mask=s2_t_mask,
                             template_token_type_ids=s2_t_segment_id)

    claim_vec = s1_embedding[0].cpu().numpy()
    sent_vec = s2_embedding[0].cpu().numpy()
    cos_sim = np.dot(claim_vec, sent_vec) / (np.linalg.norm(claim_vec) * np.linalg.norm(sent_vec))
    return cos_sim.item()


def calc_loss(embed1, embed2, temperature=0.05):
    # 这里归一化是为了后面计算cos方便
    embed1 = torch.div(embed1, torch.norm(embed1, dim=1).reshape(-1, 1))
    embed2 = torch.div(embed2, torch.norm(embed2, dim=1).reshape(-1, 1))

    batch_size, embed_dim = embed1.size()

    # 正样本之间相似距离
    batch_pos = torch.exp(torch.div(torch.bmm(embed1.view(batch_size, 1, embed_dim),
                                              embed2.view(batch_size, embed_dim, 1)).view(batch_size, 1),
                                    temperature))

    # 所有样本两两之间的相似距离
    batch_all = torch.sum(torch.exp(torch.div(
        torch.mm(embed1.view(batch_size, embed_dim), torch.t(embed2)), temperature)), dim=1)

    loss = torch.mean(-torch.log(torch.div(batch_pos, batch_all)))
    return loss


if __name__ == '__main__':
    tokenizer = BertTokenizer.from_pretrained(args.bert_pretrain_path)
    tokenizer.add_special_tokens({'additional_special_tokens': ['[X]']}) # 加入一个特殊token: [X]
    mask_id = tokenizer.mask_token_id

    train_df = load_data(args.train_data_path, tokenizer)
    train_dataset = SentDataSet(train_df, tokenizer)
    train_dataloader = DataLoader(train_dataset,
                                  batch_size=args.train_batch_size,
                                  collate_fn=collate_func)

    num_train_steps = int(
        len(train_dataset) / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

    model = PromptBERT(mask_id=mask_id)

    if torch.cuda.is_available():
        model.cuda()

    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    warmup_steps = 0.05 * num_train_steps
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=1e-8)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps,
        num_training_steps=num_train_steps)
    model.bert.resize_token_embeddings(len(tokenizer))

    # Training
    if args.train:
        print('GPU:', torch.cuda.is_available())
        for epoch in range(args.num_train_epochs):
            model.train()
            for step, batch in enumerate(tqdm(train_dataloader, desc="Train")):
                if torch.cuda.is_available():
                    batch = (t.cuda() for t in batch)
                (sent_prompt1_input_ids, sent_prompt1_attention_mask, sent_prompt1_token_type_ids,
                 sent_template1_input_ids, sent_template1_attention_mask, sent_template1_token_type_ids,
                 sent_prompt2_input_ids, sent_prompt2_attention_mask, sent_prompt2_token_type_ids,
                 sent_template2_input_ids, sent_template2_attention_mask, sent_template2_token_type_ids) = batch

                prompt_embedding0 = model(prompt_input_ids=sent_prompt1_input_ids,
                                          prompt_attention_mask=sent_prompt1_attention_mask,
                                          prompt_token_type_ids=sent_prompt1_token_type_ids,
                                          template_input_ids=sent_template1_input_ids,
                                          template_attention_mask=sent_template1_attention_mask,
                                          template_token_type_ids=sent_template1_token_type_ids)
                prompt_embedding1 = model(prompt_input_ids=sent_prompt2_input_ids,
                                          prompt_attention_mask=sent_prompt2_attention_mask,
                                          prompt_token_type_ids=sent_prompt2_token_type_ids,
                                          template_input_ids=sent_template2_input_ids,
                                          template_attention_mask=sent_template2_attention_mask,
                                          template_token_type_ids=sent_template2_token_type_ids)

                loss = calc_loss(prompt_embedding0, prompt_embedding1)
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                # print('Epoch:{}, Step:{}, Loss:{:10f}'.format(epoch, step, loss))

                loss.backward()
                # nn.utils.clip_grad_norm(model.parameters(), max_norm=20, norm_type=2)   # 是否进行梯度裁剪
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()
        model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
        torch.save(model_to_save.state_dict(), f'./checkpoint/train_claim_sent.ckpt')

    # Evaluate
    if args.eval:
        # Get similar sentence
        get_similar_sentence()