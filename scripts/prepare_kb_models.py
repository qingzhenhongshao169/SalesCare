"""Prepare CPU ONNX models once; credentials are never involved."""
import os
import sys
from pathlib import Path
os.environ.setdefault('HF_HUB_DISABLE_XET','1')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mvp.knowledge_engine import local_embedding,local_reranker,DEFAULT
print('Preparing Chinese embedding model...',flush=True)
model=local_embedding(DEFAULT['embedding_model'])
result=list(model.embed(['产品说明与售后服务']))
print('Embedding verified, dimension '+str(len(result[0])),flush=True)
print('Preparing multilingual reranker...',flush=True)
rank=local_reranker(DEFAULT['rerank_model'])
scores=list(rank.rerank('如何查找售后说明',['售后服务请参考产品说明书。','公司历史介绍。']))
print('Reranker verified: '+str(len(scores))+' scores.',flush=True)
