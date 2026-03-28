"""
实体读取与过滤服务（Neo4j版本）
从Neo4j图谱中读取节点，筛选出符合预定义实体类型的节点
"""

import time
from typing import Dict, Any, List, Optional, Set, Callable, TypeVar
from dataclasses import dataclass, field

from neo4j import GraphDatabase

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.entity_reader')

T = TypeVar('T')


@dataclass
class EntityNode:
    """实体节点数据结构"""
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]
    related_edges: List[Dict[str, Any]] = field(default_factory=list)
    related_nodes: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes,
            "related_edges": self.related_edges,
            "related_nodes": self.related_nodes,
        }
    
    def get_entity_type(self) -> Optional[str]:
        """获取实体类型（排除默认的Entity标签）"""
        for label in self.labels:
            if label not in ["Entity", "Node"]:
                return label
        return None


@dataclass
class FilteredEntities:
    """过滤后的实体集合"""
    entities: List[EntityNode]
    entity_types: Set[str]
    total_count: int
    filtered_count: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "entities": [e.to_dict() for e in self.entities],
            "entity_types": list(self.entity_types),
            "total_count": self.total_count,
            "filtered_count": self.filtered_count,
        }


class EntityReader:
    """
    实体读取与过滤服务（Neo4j版本）
    
    替代原来的 ZepEntityReader，接口保持兼容
    """
    
    def __init__(self, api_key: Optional[str] = None):
        """兼容旧接口，api_key参数被忽略"""
        self.driver = GraphDatabase.driver(
            Config.NEO4J_URI,
            auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
            connection_timeout=Config.NEO4J_CONNECTION_TIMEOUT,
            max_connection_lifetime=Config.NEO4J_MAX_CONNECTION_LIFETIME,
            connection_acquisition_timeout=Config.NEO4J_CONNECTION_ACQUISITION_TIMEOUT,
        )
    
    def __del__(self):
        if hasattr(self, 'driver') and self.driver:
            try:
                self.driver.close()
            except Exception:
                pass
    
    def get_all_nodes(self, graph_id: str) -> List[Dict[str, Any]]:
        """获取图谱的所有节点"""
        logger.info(f"获取图谱 {graph_id} 的所有节点...")
        
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (n:Entity {graph_id: $graph_id})
                RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels,
                       n.summary AS summary, n.attributes AS attributes
                """,
                graph_id=graph_id
            )
            
            nodes_data = []
            for record in result:
                nodes_data.append({
                    "uuid": record["uuid"] or "",
                    "name": record["name"] or "",
                    "labels": record["labels"] or [],
                    "summary": record["summary"] or "",
                    "attributes": record["attributes"] or {},
                })
        
        logger.info(f"共获取 {len(nodes_data)} 个节点")
        return nodes_data

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        """获取图谱的所有边"""
        logger.info(f"获取图谱 {graph_id} 的所有边...")
        
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (s:Entity {graph_id: $graph_id})-[r]->(t:Entity {graph_id: $graph_id})
                WHERE NOT type(r) IN ['CONTAINS']
                RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact,
                       s.uuid AS source_node_uuid, t.uuid AS target_node_uuid,
                       r.attributes AS attributes
                """,
                graph_id=graph_id
            )
            
            edges_data = []
            for record in result:
                edges_data.append({
                    "uuid": record["uuid"] or "",
                    "name": record["name"] or "",
                    "fact": record["fact"] or "",
                    "source_node_uuid": record["source_node_uuid"] or "",
                    "target_node_uuid": record["target_node_uuid"] or "",
                    "attributes": record["attributes"] or {},
                })
        
        logger.info(f"共获取 {len(edges_data)} 条边")
        return edges_data
    
    def get_node_edges(self, node_uuid: str) -> List[Dict[str, Any]]:
        """获取指定节点的所有相关边"""
        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (s:Entity)-[r]->(t:Entity)
                    WHERE (s.uuid = $uuid OR t.uuid = $uuid)
                      AND NOT type(r) IN ['CONTAINS']
                    RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact,
                           s.uuid AS source_node_uuid, t.uuid AS target_node_uuid,
                           r.attributes AS attributes
                    """,
                    uuid=node_uuid
                )
                
                edges_data = []
                for record in result:
                    edges_data.append({
                        "uuid": record["uuid"] or "",
                        "name": record["name"] or "",
                        "fact": record["fact"] or "",
                        "source_node_uuid": record["source_node_uuid"] or "",
                        "target_node_uuid": record["target_node_uuid"] or "",
                        "attributes": record["attributes"] or {},
                    })
                return edges_data
        except Exception as e:
            logger.warning(f"获取节点 {node_uuid} 的边失败: {str(e)}")
            return []
    
    def filter_defined_entities(
        self, 
        graph_id: str,
        defined_entity_types: Optional[List[str]] = None,
        enrich_with_edges: bool = True
    ) -> FilteredEntities:
        """筛选出符合预定义实体类型的节点"""
        logger.info(f"开始筛选图谱 {graph_id} 的实体...")
        
        all_nodes = self.get_all_nodes(graph_id)
        total_count = len(all_nodes)
        all_edges = self.get_all_edges(graph_id) if enrich_with_edges else []
        node_map = {n["uuid"]: n for n in all_nodes}
        
        filtered_entities = []
        entity_types_found = set()
        
        for node in all_nodes:
            labels = node.get("labels", [])
            custom_labels = [l for l in labels if l not in ["Entity", "Node"]]
            
            if not custom_labels:
                continue
            
            if defined_entity_types:
                matching_labels = [l for l in custom_labels if l in defined_entity_types]
                if not matching_labels:
                    continue
                entity_type = matching_labels[0]
            else:
                entity_type = custom_labels[0]
            
            entity_types_found.add(entity_type)
            
            entity = EntityNode(
                uuid=node["uuid"],
                name=node["name"],
                labels=labels,
                summary=node["summary"],
                attributes=node["attributes"],
            )
            
            if enrich_with_edges:
                related_edges = []
                related_node_uuids = set()
                
                for edge in all_edges:
                    if edge["source_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "outgoing",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "target_node_uuid": edge["target_node_uuid"],
                        })
                        related_node_uuids.add(edge["target_node_uuid"])
                    elif edge["target_node_uuid"] == node["uuid"]:
                        related_edges.append({
                            "direction": "incoming",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "source_node_uuid": edge["source_node_uuid"],
                        })
                        related_node_uuids.add(edge["source_node_uuid"])
                
                entity.related_edges = related_edges
                
                related_nodes = []
                for related_uuid in related_node_uuids:
                    if related_uuid in node_map:
                        related_node = node_map[related_uuid]
                        related_nodes.append({
                            "uuid": related_node["uuid"],
                            "name": related_node["name"],
                            "labels": related_node["labels"],
                            "summary": related_node.get("summary", ""),
                        })
                entity.related_nodes = related_nodes
            
            filtered_entities.append(entity)
        
        logger.info(f"筛选完成: 总节点 {total_count}, 符合条件 {len(filtered_entities)}, "
                   f"实体类型: {entity_types_found}")
        
        return FilteredEntities(
            entities=filtered_entities,
            entity_types=entity_types_found,
            total_count=total_count,
            filtered_count=len(filtered_entities),
        )
    
    def get_entity_with_context(
        self, 
        graph_id: str, 
        entity_uuid: str
    ) -> Optional[EntityNode]:
        """获取单个实体及其完整上下文"""
        try:
            with self.driver.session() as session:
                result = session.run(
                    """
                    MATCH (n:Entity {uuid: $uuid, graph_id: $graph_id})
                    RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels,
                           n.summary AS summary, n.attributes AS attributes
                    """,
                    uuid=entity_uuid,
                    graph_id=graph_id
                )
                record = result.single()
                if not record:
                    return None
            
            edges = self.get_node_edges(entity_uuid)
            all_nodes = self.get_all_nodes(graph_id)
            node_map = {n["uuid"]: n for n in all_nodes}
            
            related_edges = []
            related_node_uuids = set()
            
            for edge in edges:
                if edge["source_node_uuid"] == entity_uuid:
                    related_edges.append({
                        "direction": "outgoing",
                        "edge_name": edge["name"],
                        "fact": edge["fact"],
                        "target_node_uuid": edge["target_node_uuid"],
                    })
                    related_node_uuids.add(edge["target_node_uuid"])
                else:
                    related_edges.append({
                        "direction": "incoming",
                        "edge_name": edge["name"],
                        "fact": edge["fact"],
                        "source_node_uuid": edge["source_node_uuid"],
                    })
                    related_node_uuids.add(edge["source_node_uuid"])
            
            related_nodes = []
            for related_uuid in related_node_uuids:
                if related_uuid in node_map:
                    related_node = node_map[related_uuid]
                    related_nodes.append({
                        "uuid": related_node["uuid"],
                        "name": related_node["name"],
                        "labels": related_node["labels"],
                        "summary": related_node.get("summary", ""),
                    })
            
            return EntityNode(
                uuid=record["uuid"] or "",
                name=record["name"] or "",
                labels=record["labels"] or [],
                summary=record["summary"] or "",
                attributes=record["attributes"] or {},
                related_edges=related_edges,
                related_nodes=related_nodes,
            )
            
        except Exception as e:
            logger.error(f"获取实体 {entity_uuid} 失败: {str(e)}")
            return None
    
    def get_entities_by_type(
        self, 
        graph_id: str, 
        entity_type: str,
        enrich_with_edges: bool = True
    ) -> List[EntityNode]:
        """获取指定类型的所有实体"""
        result = self.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=[entity_type],
            enrich_with_edges=enrich_with_edges
        )
        return result.entities


# 兼容旧导入名
ZepEntityReader = EntityReader
