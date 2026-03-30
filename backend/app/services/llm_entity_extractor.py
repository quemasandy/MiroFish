"""
LLM实体提取服务
使用LLM从文本中提取实体和关系，替代Zep Cloud的自动提取功能
"""

import json
from typing import Dict, Any, List, Optional
from ..utils.llm_client import LLMClient
from ..utils.logger import get_logger

logger = get_logger('mirofish.llm_entity_extractor')

EXTRACTION_SYSTEM_PROMPT = """你是一个专业的知识图谱实体/关系提取专家。

给定一段文本和一组预定义的实体类型与关系类型，你需要从文本中精确提取符合这些类型的实体和关系。

**重要：你必须输出有效的JSON格式数据，不要输出任何其他内容。**

## 输出格式

```json
{
    "entities": [
        {
            "name": "实体名称",
            "entity_type": "预定义的实体类型名称",
            "attributes": {
                "属性名": "属性值"
            },
            "summary": "简短描述这个实体"
        }
    ],
    "relationships": [
        {
            "source": "源实体名称",
            "target": "目标实体名称",
            "relation_type": "预定义的关系类型名称",
            "fact": "描述这个关系的事实陈述"
        }
    ]
}
```

## 提取规则

1. **实体名称**：使用文本中出现的原始名称，保持一致性
2. **实体类型**：必须是预定义类型之一，如果不匹配任何具体类型，使用 Person 或 Organization 兜底
3. **关系类型**：必须是预定义关系类型之一
4. **去重**：相同名称的实体只提取一次
5. **事实陈述**：用一句自然语言描述关系，如"张三在清华大学读书"
6. **属性**：尽可能从文本中提取实体的属性信息
"""


class LLMEntityExtractor:
    """
    使用LLM从文本中提取实体和关系
    替代Zep Cloud的自动提取功能
    """
    
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient(stage="graph_extraction")
    
    def extract(
        self,
        text: str,
        ontology: Dict[str, Any],
        chunk_index: int = 0,
        total_chunks: int = 1,
    ) -> Dict[str, Any]:
        """
        从文本中提取实体和关系
        
        Args:
            text: 输入文本
            ontology: 本体定义（entity_types, edge_types）
            chunk_index: 当前chunk索引（用于日志）
            total_chunks: 总chunk数量
            
        Returns:
            {"entities": [...], "relationships": [...]}
        """
        # 构建用户消息
        user_message = self._build_extraction_prompt(text, ontology)
        
        messages = [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]
        
        try:
            result = self.llm_client.chat_json(
                messages=messages,
                temperature=0.1,
                max_tokens=4096
            )
            
            # 验证结果
            entities = result.get("entities", [])
            relationships = result.get("relationships", [])
            
            logger.debug(
                f"Chunk {chunk_index+1}/{total_chunks}: "
                f"提取 {len(entities)} 个实体, {len(relationships)} 个关系"
            )
            
            return {
                "entities": entities,
                "relationships": relationships
            }
            
        except Exception as e:
            logger.warning(f"Chunk {chunk_index+1}/{total_chunks} 实体提取失败: {e}")
            return {"entities": [], "relationships": []}
    
    def _build_extraction_prompt(self, text: str, ontology: Dict[str, Any]) -> str:
        """构建提取提示词"""
        # 格式化实体类型信息
        entity_types_info = []
        for et in ontology.get("entity_types", []):
            attrs = [a["name"] for a in et.get("attributes", [])]
            attrs_str = f" (属性: {', '.join(attrs)})" if attrs else ""
            entity_types_info.append(
                f"- **{et['name']}**: {et.get('description', '')}{attrs_str}"
            )
        
        # 格式化关系类型信息
        edge_types_info = []
        for edge in ontology.get("edge_types", []):
            st_info = []
            for st in edge.get("source_targets", []):
                st_info.append(f"{st['source']} → {st['target']}")
            st_str = f" ({', '.join(st_info)})" if st_info else ""
            edge_types_info.append(
                f"- **{edge['name']}**: {edge.get('description', '')}{st_str}"
            )
        
        return f"""## 预定义实体类型

{chr(10).join(entity_types_info)}

## 预定义关系类型

{chr(10).join(edge_types_info)}

## 待提取文本

{text}

请根据上述预定义的实体类型和关系类型，从文本中提取所有实体和关系。
只提取文本中明确提到的信息，不要推测。
"""
