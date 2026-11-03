"""超图模块：摘要提取、超图提取、A-C 优化、跨文档超边、实体合并"""
from .summary import run_pipeline as extract_pipeline
from .refine import run_actor_critic_loop as refine_hyperedges
