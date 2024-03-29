#coding=utf8
import torch, math
import torch.nn as nn
from model.model_utils import Registrable, clones, FFN
from nsts.relation_utils import ENCODER_RELATIONS


@Registrable.register('rgatsql')
class RGATHiddenLayer(nn.Module):
    """ A stacked layers of relational graph attention network~(RGAT).
    """
    def __init__(self, args, tranx):
        super(RGATHiddenLayer, self).__init__()
        hs, hd, rn = args.encoder_hidden_size, args.num_heads, len(ENCODER_RELATIONS)
        self.num_layers, self.num_heads = args.encoder_num_layers, args.num_heads
        self.relation_embed_k, self.relation_embed_v = None, None
        pad_idx = ENCODER_RELATIONS.index('padding-padding')
        self.relation_embed_k = nn.Embedding(rn, hs // hd, padding_idx=pad_idx)
        self.relation_embed_v = nn.Embedding(rn, hs // hd, padding_idx=pad_idx)
        gnn_module = Registrable.by_name('rgat_layer')(hs, hd, dropout=args.dropout)
        self.gnn_layers = clones(gnn_module, self.num_layers)


    def forward(self, inputs, batch):
        """ Jointly encode question nodes and ontology nodes via Relational Graph Attention Network
        @args:
            inputs: torch.FloatTensor, encoded representation from PLM, bs x max_len x hs
        @return:
            outputs: torch.FloatTensor, bs x max_len x hs, max_len is sum of the maximum of question_nodes and schema items
        """
        outputs = inputs
        rel = batch.encoder_relations
        rel_k = self.relation_embed_k(rel).unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
        rel_v = self.relation_embed_v(rel).unsqueeze(1).expand(-1, self.num_heads, -1, -1, -1)
        for i in range(self.num_layers):
            outputs = self.gnn_layers[i](outputs, batch.encoder_relations_mask, rel_k, rel_v)
        return outputs


@Registrable.register('rgat_layer')
class RGATLayer(nn.Module):
    """ Encode question nodes and schema nodes via relational graph attention network, parameters are shared for these two types.
    """
    def __init__(self, hidden_size=512, num_heads=8, dropout=0.2):
        super(RGATLayer, self).__init__()
        assert hidden_size % num_heads == 0, 'Hidden size is not divisible by num of heads'
        self.hidden_size, self.num_heads = hidden_size, num_heads
        self.query = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.key = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.value = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.scale_factor = math.sqrt(self.hidden_size // self.num_heads)
        self.concat_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.feedforward = FFN(self.hidden_size)
        self.layernorm = nn.LayerNorm(self.hidden_size)
        self.dropout_layer = nn.Dropout(p=dropout)


    def forward(self, inputs, mask, rel_k, rel_v):

        def calculate_outputs(inputs, mask):
            bs, l = inputs.size(0), inputs.size(1)
            q = self.query(self.dropout_layer(inputs))
            k = self.key(self.dropout_layer(inputs))
            v = self.value(self.dropout_layer(inputs))
            q = q.view(bs, l, self.num_heads, -1).transpose(1, 2).unsqueeze(3) # q: bsize x num_heads x seqlen x 1 x dim
            # k and v: bsize x num_heads x seqlen x seqlen x dim
            k = k.view(bs, l, self.num_heads, -1).transpose(1, 2).unsqueeze(2).expand(bs, self.num_heads, l, l, -1)
            v = v.view(bs, l, self.num_heads, -1).transpose(1, 2).unsqueeze(2).expand(bs, self.num_heads, l, l, -1)
            k, v = k + rel_k, v + rel_v
            # e: bsize x heads x seqlen x seqlen
            e = (torch.matmul(q, k.transpose(-1, -2)) / self.scale_factor).squeeze(-2)
            e = e.masked_fill_(mask.unsqueeze(1), -1e20) # mask no-relation
            a = torch.softmax(e, dim=-1)
            outputs = torch.matmul(a.unsqueeze(-2), v).squeeze(-2)
            outputs = outputs.transpose(1, 2).contiguous().view(bs, l, -1)
            outputs = self.concat_affine(outputs)
            outputs = self.layernorm(inputs + outputs)
            return outputs

        return self.feedforward(calculate_outputs(inputs, mask))


@Registrable.register('irnet')
class IRNetHiddenLayer(nn.Module):

    def __init__(self, args, tranx):
        super(IRNetHiddenLayer, self).__init__()
        self.num_layers = args.encoder_num_layers
        gnn_module = Registrable.by_name('irnet_layer')(args.encoder_hidden_size, args.num_heads, dropout=args.dropout)
        self.gnn_layers = clones(gnn_module, self.num_layers)


    def forward(self, inputs, batch):
        outputs = inputs
        for i in range(self.num_layers):
            outputs = self.gnn_layers[i](inputs, batch.encoder_relations_mask)
        return outputs


@Registrable.register('irnet_layer')
class IRNetLayer(nn.Module):

    def __init__(self, hidden_size=512, num_heads=8, dropout=0.2):
        super(IRNetLayer, self).__init__()
        assert hidden_size % num_heads == 0, 'Hidden size is not divisible by num of heads'
        self.hidden_size, self.num_heads = hidden_size, num_heads
        self.query = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.key = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.value = nn.Linear(self.hidden_size, self.hidden_size, bias=False)
        self.scale_factor = math.sqrt(self.hidden_size // self.num_heads)
        self.concat_affine = nn.Linear(self.hidden_size, self.hidden_size, bias=True)
        self.feedforward = FFN(self.hidden_size)
        self.layernorm = nn.LayerNorm(self.hidden_size)
        self.dropout_layer = nn.Dropout(p=dropout)


    def forward(self, inputs, mask):
        """ The official implementaion of Tranformer module is complicated regarding the usage of mask matrix.
        Re-implement it with self-defined modules.
        """
        def calculate_outputs(inputs, mask):
            bsize, seqlen = inputs.size(0), inputs.size(1)
            # bsize x num_heads x seqlen x dim
            q = self.query(self.dropout_layer(inputs)).view(bsize, seqlen, self.num_heads, -1).transpose(1, 2)
            k = self.key(self.dropout_layer(inputs)).view(bsize, seqlen, self.num_heads, -1).transpose(1, 2)
            v = self.value(self.dropout_layer(inputs)).view(bsize, seqlen, self.num_heads, -1).transpose(1, 2)
            # e: bsize x num_heads x seqlen x seqlen
            e = (torch.matmul(q, k.transpose(-1, -2)) / self.scale_factor)
            e = e.masked_fill_(mask.unsqueeze(1), -1e20) # mask padding-relation
            a = torch.softmax(e, dim=-1)
            outputs = torch.matmul(a, v)
            outputs = outputs.transpose(1, 2).contiguous().view(bsize, seqlen, -1)
            outputs = self.concat_affine(outputs)
            outputs = self.layernorm(inputs + outputs)
            return outputs

        return self.feedforward(calculate_outputs(inputs, mask))


@Registrable.register('none')
class NoneHiddenLayer(nn.Module):
    """ Directly use the output of PLM without passing through a graph neural network.
    """
    def __init__(self, args, tranx):
        super(NoneHiddenLayer, self).__init__()
        self.hidden_size = args.encoder_hidden_size
        self.num_layers, self.num_heads = args.encoder_num_layers, args.num_heads
        encoder_layer = nn.TransformerEncoderLayer(self.hidden_size, self.num_heads, self.hidden_size * 4, dropout=args.dropout)
        self.encoder_layers = nn.TransformerEncoder(encoder_layer, self.num_layers)


    def forward(self, inputs, batch):
        return self.encoder_layers(inputs.transpose(0, 1), src_key_padding_mask=~ batch.mask).transpose(0, 1)
