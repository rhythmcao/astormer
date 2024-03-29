#coding=utf8
import torch, math
import torch.nn as nn
import torch.nn.functional as F
from model.model_utils import clones, FFN
from nsts.relation_utils import ASTRelation


class DecoupledAstormer(nn.Module):
    """ Different from ASTormer, we calculate the self-attention among input nodes and cross-attention between
    inputs nodes and all previous output actions.
    """
    def __init__(self, hidden_size, num_layers=1, num_heads=8, dropout=0.) -> None:
        super(DecoupledAstormer, self).__init__()
        self.hidden_size, self.num_heads = hidden_size, num_heads
        assert self.hidden_size % self.num_heads == 0
        rn = len(ASTRelation.DECODER_RELATIONS)
        self.pad_idx = ASTRelation.DECODER_RELATIONS.index('padding-padding')
        self.self_embed_k = nn.Embedding(rn, self.hidden_size // self.num_heads, padding_idx=self.pad_idx)
        self.self_embed_v = nn.Embedding(rn, self.hidden_size // self.num_heads, padding_idx=self.pad_idx)
        # self.tgt_embed_k = nn.Embedding(rn, self.hidden_size // self.num_heads, padding_idx=self.pad_idx)
        # self.tgt_embed_v = nn.Embedding(rn, self.hidden_size // self.num_heads, padding_idx=self.pad_idx)
        self.tgt_embed_k, self.tgt_embed_v = self.self_embed_k, self.self_embed_v
        self.num_layers = num_layers
        decoder_layer = DecoupledAstormerLayer(self.hidden_size, self.num_heads, dropout)
        self.decoder_layers = nn.ModuleList(clones(decoder_layer, self.num_layers))


    def forward(self, q, prev, kv, rel_ids=None, cross_rel_ids=None, enc_mask=None, return_attention_weights=False):
        """ A stacked modules of Astormer layers.
        @args:
            q: query vector, bs x tgt_len x hs
            prev: previous action embeddings, bs x tgt_len x hs
            kv: encoded representation, bs x src_len x hs
            rel_ids: relations for input nodes, bs x tgt_len x tgt_len
            cross_rel_ids: relations between input nodes and output actions, bs x tgt_len x tgt_len
            enc_mask: mask for input, bs x src_len
        @return:
            o: output vectors, bs x tgt_len x hs
        """
        o, future_mask = q, torch.tril(torch.ones((q.size(0), q.size(1), q.size(1)), dtype=torch.bool, device=q.device))
        self_k, self_v, tgt_k, tgt_v = None, None, None, None
        self_mask, tgt_mask = future_mask, future_mask
        if rel_ids is not None:
            self_k, self_v = self.self_embed_k(rel_ids), self.self_embed_v(rel_ids)
            self_k = self_k.unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
            self_v = self_v.unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
            self_mask = (rel_ids != self.pad_idx) & future_mask
        if cross_rel_ids is not None:
            tgt_k, tgt_v = self.tgt_embed_k(cross_rel_ids), self.tgt_embed_v(cross_rel_ids)
            tgt_k = tgt_k.unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
            tgt_v = tgt_v.unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
            tgt_mask = (cross_rel_ids != self.pad_idx) & future_mask
    
        attention_weights = []
        for i in range(self.num_layers):
            o = self.decoder_layers[i](o, prev, kv, (self_k, self_v, self_mask), (tgt_k, tgt_v, tgt_mask), enc_mask, return_attention_weights)
            if return_attention_weights:
                o, attention_weight = o
                attention_weights.append(attention_weight)
        if return_attention_weights:
            return o, torch.stack(attention_weights, dim=1)
        return o


class DecoupledAstormerLayer(nn.Module):

    def __init__(self, hidden_size, num_heads=8, dropout=0.) -> None:
        super(DecoupledAstormerLayer, self).__init__()
        self.hidden_size, self.num_heads = hidden_size, num_heads
        self.scale_factor = math.sqrt(self.hidden_size // self.num_heads)
        self.dropout_layer = nn.Dropout(p=dropout)
        self.self_q_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.self_k_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.self_v_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.self_o_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.self_layer_norm = nn.LayerNorm(self.hidden_size)
        self.tgt_q_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.tgt_k_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.tgt_v_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.tgt_o_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.tgt_layer_norm = nn.LayerNorm(self.hidden_size)
        self.cross_q_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.cross_k_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.cross_v_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.cross_o_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.cross_layer_norm = nn.LayerNorm(self.hidden_size)
        self.feedforward = FFN(self.hidden_size)


    def forward(self, q, prev, kv, self_rels, tgt_rels, enc_mask=None, return_attention_weights=False):
        """ Three modules: masked AST structure-aware multi-head self-attention, multi-head cross-attention and feedforward module.
        @args:
            q: input query vector, bs x tgt_len x hidden_size
            prev: previous action embeddings, bs x tgt_len x hidden_size
            kv: encoded representations, bs x src_len x hidden_size
            self_rels: triple of (relation key embeddings, relation value embeddings, relation mask) among input nodes
            tgt_rels: similar to self_rels, but for input nodes and shifted output actions
            enc_mask: mask for input, torch.BoolTensor, bs x src_len, 0 -> padding position, o.w. used position
        """
        attention_weights = []
        def calculate_self_attention_with_relation(q, rel_k, rel_v, mask):
            bs, l = q.size(0), q.size(1)
            # bs x head x l x dim
            Q = self.self_q_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            K = self.self_k_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            V = self.self_v_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            K = K.unsqueeze(2).expand(bs, self.num_heads, l, l, -1) # bs x head x l x l x dim
            V = V.unsqueeze(2).expand(bs, self.num_heads, l, l, -1)
            K_rel, V_rel = K + rel_k, V + rel_v
            e = (torch.matmul(Q.unsqueeze(3), K_rel.transpose(-1, -2)) / self.scale_factor).squeeze(-2)
            if mask is not None:
                e = e.masked_fill_(~ mask.unsqueeze(1), -1e10) # bs x head x l x l
            a = torch.softmax(e, dim=-1)
            o = torch.matmul(a.unsqueeze(-2), V_rel).squeeze(-2) # bs x head x l x dim
            o = o.transpose(1, 2).contiguous().view(bs, l, -1)
            return self.self_layer_norm(q + self.self_o_affine(o)), a

        def calculate_self_attention(q, mask):
            bs, l = q.size(0), q.size(1)
            # bs x head x len x dim
            Q = self.self_q_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1)
            K = self.self_k_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1)
            V = self.self_v_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1)
            e = torch.einsum('bthd,bshd->btsh', Q, K) / self.scale_factor
            if mask is not None:
                e = e.masked_fill_(~ mask.unsqueeze(-1), -1e10)
            a = torch.softmax(e, dim=2)
            o = torch.einsum('btsh,bshd->bthd', a, V).reshape(bs, l, -1)
            return self.self_layer_norm(q + self.self_o_affine(o)), a

        def calculate_tgt_attention_with_relation(q, kv, rel_k, rel_v, mask):
            bs, l = q.size(0), q.size(1)
            # bs x head x l x dim
            Q = self.tgt_q_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            K = self.tgt_k_affine(self.dropout_layer(kv)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            V = self.tgt_v_affine(self.dropout_layer(kv)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            K = K.unsqueeze(2).expand(bs, self.num_heads, l, l, -1) # bs x head x l x l x dim
            V = V.unsqueeze(2).expand(bs, self.num_heads, l, l, -1)
            K_rel, V_rel = K + rel_k, V + rel_v
            e = (torch.matmul(Q.unsqueeze(3), K_rel.transpose(-1, -2)) / self.scale_factor).squeeze(-2)
            if mask is not None:
                e = e.masked_fill_(~ mask.unsqueeze(1), -1e10) # bs x head x l x l
            a = torch.softmax(e, dim=-1)
            o = torch.matmul(a.unsqueeze(-2), V_rel).squeeze(-2) # bs x head x l x dim
            o = o.transpose(1, 2).contiguous().view(bs, l, -1)
            return self.tgt_layer_norm(q + self.tgt_o_affine(o))

        def calculate_tgt_attention(q, kv, mask):
            Q = self.tgt_q_affine(self.dropout_layer(q)).reshape(q.size(0), q.size(1), self.num_heads, -1)
            K = self.tgt_k_affine(self.dropout_layer(kv)).reshape(kv.size(0), kv.size(1), self.num_heads, -1)
            V = self.tgt_v_affine(self.dropout_layer(kv)).reshape(kv.size(0), kv.size(1), self.num_heads, -1)
            e = torch.einsum('bthd,bshd->btsh', Q, K) / self.scale_factor
            if mask is not None:
                e = e.masked_fill_(~ mask.unsqueeze(-1), -1e10)
            a = torch.softmax(e, dim=2)
            o = torch.einsum('btsh,bshd->bthd', a, V).reshape(q.size(0), q.size(1), -1)
            return self.tgt_layer_norm(q + self.tgt_o_affine(o))

        def calculate_cross_attention(q, kv, mask):
            Q = self.cross_q_affine(self.dropout_layer(q)).reshape(q.size(0), q.size(1), self.num_heads, -1)
            K = self.cross_k_affine(self.dropout_layer(kv)).reshape(kv.size(0), kv.size(1), self.num_heads, -1)
            V = self.cross_v_affine(self.dropout_layer(kv)).reshape(kv.size(0), kv.size(1), self.num_heads, -1)
            e = torch.einsum('bthd,bshd->btsh', Q, K) / self.scale_factor
            if mask is not None:
                e = e.masked_fill_(~ mask.unsqueeze(1).unsqueeze(-1), -1e10)
            a = torch.softmax(e, dim=2)
            o = torch.einsum('btsh,bshd->bthd', a, V).reshape(q.size(0), q.size(1), -1)
            return self.cross_layer_norm(q + self.cross_o_affine(o))

        self_k, self_v, self_mask = self_rels
        if self_k is not None:
            o, attention_weights = calculate_self_attention_with_relation(q, self_k, self_v, self_mask)
        else: o, attention_weights = calculate_self_attention(q, self_mask)

        tgt_k, tgt_v, tgt_mask = tgt_rels
        if tgt_k is not None:
            o = calculate_tgt_attention_with_relation(q, prev, tgt_k, tgt_v, tgt_mask)
        else: o = calculate_tgt_attention(o, prev, tgt_mask)

        o = calculate_cross_attention(o, kv, enc_mask)
        if return_attention_weights: return self.feedforward(o), attention_weights
        return self.feedforward(o)


class Astormer(nn.Module):

    def __init__(self, hidden_size, num_layers=1, num_heads=8, dropout=0.) -> None:
        super(Astormer, self).__init__()
        self.hidden_size, self.num_heads = hidden_size, num_heads
        assert self.hidden_size % self.num_heads == 0
        rn = len(ASTRelation.DECODER_RELATIONS)
        self.pad_idx = ASTRelation.DECODER_RELATIONS.index('padding-padding')
        self.relation_embed_k = nn.Embedding(rn, self.hidden_size // self.num_heads, padding_idx=self.pad_idx)
        self.relation_embed_v = nn.Embedding(rn, self.hidden_size // self.num_heads, padding_idx=self.pad_idx)
        self.num_layers = num_layers
        decoder_layer = AstormerLayer(self.hidden_size, self.num_heads, dropout)
        self.decoder_layers = nn.ModuleList(clones(decoder_layer, self.num_layers))


    def forward(self, q, kv, rel_ids=None, enc_mask=None, return_attention_weights=False):
        """ A stacked modules of Astormer layers.
        @args:
            q: query vector, bs x tgt_len x hs
            kv: encoded representation, bs x src_len x hs
            rel_ids: relations, bs x tgt_len x tgt_len
            enc_mask: mask for input, bs x src_len
        @return:
            o: output vectors, bs x tgt_len x hs
        """
        o, rel_k, rel_v, rel_mask = q, None, None, torch.tril(torch.ones((q.size(0), q.size(1), q.size(1)), dtype=torch.bool, device=q.device))
        if rel_ids is not None:
            rel_k, rel_v = self.relation_embed_k(rel_ids), self.relation_embed_v(rel_ids)
            rel_k = rel_k.unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
            rel_v = rel_v.unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
            rel_mask = (rel_ids != self.pad_idx) & rel_mask

        attention_weights = []
        for i in range(self.num_layers):
            o = self.decoder_layers[i](o, kv, rel_k, rel_v, rel_mask, enc_mask, return_attention_weights)
            if return_attention_weights:
                o, attention_weight = o
                attention_weights.append(attention_weight)
        if return_attention_weights:
            return o, torch.stack(attention_weights, dim=1)
        return o


class AstormerLayer(nn.Module):

    def __init__(self, hidden_size, num_heads=8, dropout=0.) -> None:
        super(AstormerLayer, self).__init__()
        self.hidden_size, self.num_heads = hidden_size, num_heads
        self.scale_factor = math.sqrt(self.hidden_size // self.num_heads)
        self.dropout_layer = nn.Dropout(p=dropout)
        self.self_q_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.self_k_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.self_v_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.self_o_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.self_layer_norm = nn.LayerNorm(self.hidden_size)
        self.cross_q_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.cross_k_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.cross_v_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.cross_o_affine = nn.Linear(self.hidden_size, self.hidden_size)
        self.cross_layer_norm = nn.LayerNorm(self.hidden_size)
        self.feedforward = FFN(self.hidden_size)


    def forward(self, q, kv, rel_k, rel_v, rel_mask, enc_mask=None, return_attention_weights=False):
        """ Three modules: masked AST structure-aware multi-head self-attention, multi-head cross-attention and feedforward module.
        @args:
            q: input query vector, bs x tgt_len x hidden_size
            kv: encoded representations, bs x src_len x hidden_size
            rel_v/rel_v: relation embeddings, bs x tgt_len x tgt_len x rel_size
            rel_mask: relation mask, torch.BoolTensor, bs x tgt_len x tgt_len, 0 -> padding relation, o.w. specific relations
            enc_mask: mask for input, torch.BoolTensor, bs x src_len, 0 -> padding position, o.w. used position
        """
        def calculate_self_attention_with_relation(q):
            bs, l = q.size(0), q.size(1)
            # bs x head x l x dim
            Q = self.self_q_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            K = self.self_k_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            V = self.self_v_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1).transpose(1, 2)
            K = K.unsqueeze(2).expand(bs, self.num_heads, l, l, -1) # bs x head x l x l x dim
            V = V.unsqueeze(2).expand(bs, self.num_heads, l, l, -1)
            K_rel, V_rel = K + rel_k, V + rel_v
            e = (torch.matmul(Q.unsqueeze(3), K_rel.transpose(-1, -2)) / self.scale_factor).squeeze(-2)
            e = e.masked_fill_(~ rel_mask.unsqueeze(1), -1e10) # bs x head x l x l
            a = torch.softmax(e, dim=-1)
            o = torch.matmul(a.unsqueeze(-2), V_rel).squeeze(-2) # bs x head x l x dim
            o = o.transpose(1, 2).contiguous().view(bs, l, -1)
            return self.self_layer_norm(q + self.self_o_affine(o)), a

        def calculate_self_attention(q):
            bs, l = q.size(0), q.size(1)
            # bs x head x len x dim
            Q = self.self_q_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1)
            K = self.self_k_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1)
            V = self.self_v_affine(self.dropout_layer(q)).reshape(bs, l, self.num_heads, -1)
            e = torch.einsum('bthd,bshd->btsh', Q, K) / self.scale_factor
            e = e.masked_fill_(~ rel_mask.unsqueeze(-1), -1e10)
            a = torch.softmax(e, dim=2)
            o = torch.einsum('btsh,bshd->bthd', a, V).reshape(bs, l, -1)
            return self.self_layer_norm(q + self.self_o_affine(o)), a

        def calculate_cross_attention(q, kv):
            Q = self.cross_q_affine(self.dropout_layer(q)).reshape(q.size(0), q.size(1), self.num_heads, -1)
            K = self.cross_k_affine(self.dropout_layer(kv)).reshape(kv.size(0), kv.size(1), self.num_heads, -1)
            V = self.cross_v_affine(self.dropout_layer(kv)).reshape(kv.size(0), kv.size(1), self.num_heads, -1)
            e = torch.einsum('bthd,bshd->btsh', Q, K) / self.scale_factor
            if enc_mask is not None:
                e = e.masked_fill_(~ enc_mask.unsqueeze(1).unsqueeze(-1), -1e10)
            a = torch.softmax(e, dim=2)
            o = torch.einsum('btsh,bshd->bthd', a, V).reshape(q.size(0), q.size(1), -1)
            return self.cross_layer_norm(q + self.cross_o_affine(o))

        o, attention_weights = calculate_self_attention_with_relation(q) if rel_k is not None else calculate_self_attention(q)
        o = calculate_cross_attention(o, kv)
        if return_attention_weights: return self.feedforward(o), attention_weights
        return self.feedforward(o)


def cumsoftmax(x, dim=-1):
    return torch.cumsum(F.softmax(x, dim=dim), dim=dim)


class LinearDropConnect(nn.Linear):
    """ Used in recurrent connection dropout
    """
    def __init__(self, in_features, out_features, bias=True, dropconnect=0.):
        super(LinearDropConnect, self).__init__(in_features=in_features, out_features=out_features, bias=bias)
        self.dropconnect = dropconnect


    def sample_mask(self):
        if self.dropconnect == 0.:
            self._weight = self.weight.clone()
        else:
            mask = self.weight.new_zeros(self.weight.size(), dtype=torch.bool)
            mask.bernoulli_(self.dropconnect)
            self._weight = self.weight.masked_fill(mask, 0.)


    def forward(self, inputs, sample_mask=False):
        if self.training:
            if sample_mask:
                self.sample_mask()
            return F.linear(inputs, self._weight, self.bias) # apply the same mask to weight matrix in linear module
        else:
            return F.linear(inputs, self.weight * (1 - self.dropconnect), self.bias)


class LockedDropout(nn.Module):
    """ Used in dropout between layers
    """
    def __init__(self, hidden_size, num_layers=1, dropout=0.2):
        super(LockedDropout, self).__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout


    def sample_masks(self, x):
        self.masks = []
        for _ in range(self.num_layers - 1):
            mask = x.new_zeros(x.size(0), 1, self.hidden_size).bernoulli_(1 - self.dropout)
            mask = mask.div_(1 - self.dropout)
            mask.requires_grad = False
            self.masks.append(mask)


    def forward(self, x, layer=0, prev_idx=None):
        """ x: bsize x seqlen x hidden_size """
        if (not self.training) or self.dropout == 0. or layer == self.num_layers - 1: # output hidden states, no dropout
            return x
        mask = self.masks[layer]
        if prev_idx is None:
            mask = mask.expand_as(x)
        else:
            mask = mask[prev_idx].expand_as(x)
        return mask * x


class RecurrentNeuralNetwork(nn.Module):

    def init_hiddens(self, x):
        return x.new_zeros(self.num_layers, x.size(0), self.hidden_size), \
            x.new_zeros(self.num_layers, x.size(0), self.hidden_size)


    def forward(self, inputs, hiddens=None, start=False, prev_idx=None, layerwise=False):
        """
        @args:
            start: whether sampling locked masks for recurrent connections and between layers
            layerwise: whether return a list, results of intermediate layer outputs
        @return:
            outputs: bsize x seqlen x hidden_size
            final_hiddens: hT and cT, each of size: num_layers x bsize x hidden_size
        """
        assert inputs.dim() == 3
        if hiddens is None:
            hiddens = self.init_hiddens(inputs)
        bsize, seqlen, _ = list(inputs.size())
        prev_state = list(hiddens) # each of size: num_layers, bsize, hidden_size
        prev_layer = inputs # size: bsize, seqlen, input_size
        each_layer_outputs, final_h, final_c = [], [], []

        if self.training and start:
            for c in self.cells:
                c.sample_masks()
            self.locked_dropout.sample_masks(inputs)

        for l in range(len(self.cells)):
            curr_layer = [None] * seqlen
            curr_inputs = self.cells[l].ih(prev_layer)
            next_h, next_c = prev_state[0][l], prev_state[1][l]
            for t in range(seqlen):
                hidden, cell = self.cells[l](None, (next_h, next_c), transformed_inputs=curr_inputs[:, t])
                next_h, next_c = hidden, cell  # overwritten every timestep
                curr_layer[t] = hidden

            prev_layer = torch.stack(curr_layer, dim=1) # bsize x seqlen x hidden_size
            each_layer_outputs.append(prev_layer)
            final_h.append(next_h)
            final_c.append(next_c)
            prev_layer = self.locked_dropout(prev_layer, layer=l, prev_idx=prev_idx)

        outputs, final_hiddens = prev_layer, (torch.stack(final_h, dim=0), torch.stack(final_c, dim=0))
        if layerwise:
            return outputs, final_hiddens, each_layer_outputs
        else:
            return outputs, final_hiddens


class LSTMCell(nn.Module):

    def __init__(self, input_size, hidden_size, bias=True, dropconnect=0.):
        super(LSTMCell, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.ih = nn.Linear(input_size, hidden_size * 4, bias=bias)
        self.hh = LinearDropConnect(hidden_size, hidden_size * 4, bias=bias, dropconnect=dropconnect)
        self.drop_weight_modules = [self.hh]


    def sample_masks(self):
        for m in self.drop_weight_modules:
            m.sample_mask()


    def forward(self, inputs, hiddens, transformed_inputs=None):
        """
        @args:
            inputs: bsize x input_size
            hiddens: tuple of h0 (bsize x hidden_size) and c0 (bsize x hidden_size)
            transformed_inputs: short cut for inputs, save time if seq len is already provied in training
        @return:
            tuple of h1 (bsize x hidden_size) and c1 (bsize x hidden_size)
        """
        if transformed_inputs is None:
            transformed_inputs = self.ih(inputs)
        h0, c0 = hiddens
        gates = transformed_inputs + self.hh(h0)
        ingate, forgetgate, outgate, cell = gates.contiguous().\
            view(-1, 4, self.hidden_size).chunk(4, 1)
        forgetgate = torch.sigmoid(forgetgate.squeeze(1))
        ingate = torch.sigmoid(ingate.squeeze(1))
        cell = torch.tanh(cell.squeeze(1))
        outgate = torch.sigmoid(outgate.squeeze(1))
        c1 = forgetgate * c0 + ingate * cell
        h1 = outgate * torch.tanh(c1)
        return h1, c1


class LSTM(RecurrentNeuralNetwork):

    def __init__(self, input_size, hidden_size, num_layers=1, chunk_num=1, bias=True, dropout=0., dropconnect=0.):
        super(LSTM, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.cells = nn.ModuleList(
            [LSTMCell(input_size, hidden_size, bias, dropconnect)] +
            [LSTMCell(hidden_size, hidden_size, bias, dropconnect) for i in range(num_layers - 1)]
        )
        self.locked_dropout = LockedDropout(hidden_size, num_layers, dropout) # dropout rate between layers


class ONLSTMCell(nn.Module):

    def __init__(self, input_size, hidden_size, chunk_num=8, bias=True, dropconnect=0.2):
        super(ONLSTMCell, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.chunk_num = chunk_num # chunk_num should be divided by hidden_size
        if self.hidden_size % self.chunk_num != 0:
            raise ValueError('[Error]: chunk number must be divided by hidden size in ONLSTM Cell')
        self.chunk_size = int(hidden_size / chunk_num)

        self.ih = nn.Linear(input_size, self.chunk_size * 2 + hidden_size * 4, bias=bias)
        self.hh = LinearDropConnect(hidden_size, self.chunk_size * 2 + hidden_size * 4, bias=bias, dropconnect=dropconnect)
        self.drop_weight_modules = [self.hh]


    def sample_masks(self):
        for m in self.drop_weight_modules:
            m.sample_mask()


    def forward(self, inputs, hiddens, transformed_inputs=None):
        """
            inputs: bsize x input_size
            hiddens: tuple of h0 (bsize x hidden_size) and c0 (bsize x hidden_size)
            transformed_inputs: short cut for inputs, save time if seq len is already provied in training
            return tuple of h1 (bsize x hidden_size) and c1 (bsize x hidden_size)
        """
        if transformed_inputs is None:
            transformed_inputs = self.ih(inputs)
        h0, c0 = hiddens
        gates = transformed_inputs + self.hh(h0)
        cingate, cforgetgate = gates[:, :self.chunk_size * 2].chunk(2, 1)
        ingate, forgetgate, outgate, cell = gates[:, self.chunk_size * 2:].contiguous().\
            view(-1, self.chunk_size * 4, self.chunk_num).chunk(4, 1)

        cingate = 1. - cumsoftmax(cingate)
        cforgetgate = cumsoftmax(cforgetgate)
        cingate = cingate[:, :, None]
        cforgetgate = cforgetgate[:, :, None]

        forgetgate = torch.sigmoid(forgetgate)
        ingate = torch.sigmoid(ingate)
        cell = torch.tanh(cell)
        outgate = torch.sigmoid(outgate)

        overlap = cforgetgate * cingate
        forgetgate = forgetgate * overlap + (cforgetgate - overlap)
        ingate = ingate * overlap + (cingate - overlap)
        c0 = c0.contiguous().view(-1, self.chunk_size, self.chunk_num)
        c1 = forgetgate * c0 + ingate * cell
        h1 = outgate * torch.tanh(c1)
        return h1.contiguous().view(-1, self.hidden_size), c1.contiguous().view(-1, self.hidden_size)


class ONLSTM(RecurrentNeuralNetwork):

    def __init__(self, input_size, hidden_size, num_layers=1, chunk_num=8, bias=True, dropout=0., dropconnect=0.):
        super(ONLSTM, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.cells = nn.ModuleList(
            [ONLSTMCell(input_size, hidden_size, chunk_num, bias, dropconnect)] +
            [ONLSTMCell(hidden_size, hidden_size, chunk_num, bias, dropconnect) for i in range(num_layers - 1)]
        )
        self.locked_dropout = LockedDropout(hidden_size, num_layers, dropout) # dropout rate between layers
