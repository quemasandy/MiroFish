"""
图谱构建服务
使用Neo4j本地图数据库构建知识图谱（替代Zep Cloud）
"""

import os
import uuid
import time
import threading
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass

from neo4j import GraphDatabase

from ..config import Config
from ..models.task import TaskManager, TaskStatus
from .text_processor import TextProcessor
from .llm_entity_extractor import LLMEntityExtractor
from ..utils.logger import get_logger

logger = get_logger('mirofish.graph_builder')


@dataclass
class GraphInfo:
    """图谱信息"""
    graph_id: str
    node_count: int
    edge_count: int
    entity_types: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entity_types": self.entity_types,
        }


class GraphBuilderService:
    """
    图谱构建服务
    负责使用Neo4j构建知识图谱
    """
    
    def __init__(
        self,
        neo4j_uri: Optional[str] = None,
        neo4j_user: Optional[str] = None,
        neo4j_password: Optional[str] = None,
        # 保留api_key参数以兼容旧调用，但不再使用
        api_key: Optional[str] = None,
    ):
        self.neo4j_uri = neo4j_uri or Config.NEO4J_URI
        self.neo4j_user = neo4j_user or Config.NEO4J_USER
        self.neo4j_password = neo4j_password or Config.NEO4J_PASSWORD
        
        self.driver = GraphDatabase.driver(
            self.neo4j_uri,
            auth=(self.neo4j_user, self.neo4j_password),
            connection_timeout=Config.NEO4J_CONNECTION_TIMEOUT,
            max_connection_lifetime=Config.NEO4J_MAX_CONNECTION_LIFETIME,
            connection_acquisition_timeout=Config.NEO4J_CONNECTION_ACQUISITION_TIMEOUT,
        )
        self.task_manager = TaskManager()
        self.extractor = LLMEntityExtractor()
    
    def __del__(self):
        if hasattr(self, 'driver') and self.driver:
            try:
                self.driver.close()
            except Exception:
                pass
    
    def build_graph_async(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str = "MiroFish Graph",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 3
    ) -> str:
        """异步构建图谱"""
        task_id = self.task_manager.create_task(
            task_type="graph_build",
            metadata={
                "graph_name": graph_name,
                "chunk_size": chunk_size,
                "text_length": len(text),
            }
        )
        
        thread = threading.Thread(
            target=self._build_graph_worker,
            args=(task_id, text, ontology, graph_name, chunk_size, chunk_overlap, batch_size)
        )
        thread.daemon = True
        thread.start()
        
        return task_id
    
    def _build_graph_worker(
        self,
        task_id: str,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int
    ):
        """图谱构建工作线程"""
        try:
            self.task_manager.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                progress=5,
                message="开始构建图谱..."
            )
            
            # 1. 创建图谱
            graph_id = self.create_graph(graph_name)
            self.task_manager.update_task(
                task_id,
                progress=10,
                message=f"图谱已创建: {graph_id}"
            )
            
            # 2. 保存本体到Neo4j
            self.set_ontology(graph_id, ontology)
            self.task_manager.update_task(
                task_id,
                progress=15,
                message="本体已设置"
            )
            
            # 3. 文本分块
            chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
            total_chunks = len(chunks)
            self.task_manager.update_task(
                task_id,
                progress=20,
                message=f"文本已分割为 {total_chunks} 个块"
            )
            
            # 4. 提取实体和关系并写入Neo4j
            episode_uuids = self.add_text_batches(
                graph_id, chunks, batch_size,
                ontology=ontology,
                progress_callback=lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=20 + int(prog * 70),  # 20-90%
                    message=msg
                )
            )
            
            # 5. 获取图谱信息
            self.task_manager.update_task(
                task_id,
                progress=90,
                message="获取图谱信息..."
            )
            
            graph_info = self._get_graph_info(graph_id)
            
            # 完成
            self.task_manager.complete_task(task_id, {
                "graph_id": graph_id,
                "graph_info": graph_info.to_dict(),
                "chunks_processed": total_chunks,
            })
            
        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            self.task_manager.fail_task(task_id, error_msg)
    
    def create_graph(self, name: str) -> str:
        """创建图谱（在Neo4j中创建graph_id标记）"""
        graph_id = f"mirofish_{uuid.uuid4().hex[:16]}"
        
        with self.driver.session() as session:
            # 创建一个GraphMeta节点来存储图谱信息
            session.run(
                """
                CREATE (g:GraphMeta {
                    graph_id: $graph_id,
                    name: $name,
                    description: 'MiroFish Social Simulation Graph',
                    created_at: datetime()
                })
                """,
                graph_id=graph_id,
                name=name
            )
        
        logger.info(f"Neo4j图谱已创建: {graph_id}")
        return graph_id
    
    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]):
        """保存本体定义到Neo4j"""
        import json
        
        with self.driver.session() as session:
            session.run(
                """
                MATCH (g:GraphMeta {graph_id: $graph_id})
                SET g.ontology = $ontology_json
                """,
                graph_id=graph_id,
                ontology_json=json.dumps(ontology, ensure_ascii=False)
            )
        
        entity_count = len(ontology.get("entity_types", []))
        edge_count = len(ontology.get("edge_types", []))
        logger.info(f"本体已保存到Neo4j: {entity_count} 实体类型, {edge_count} 关系类型")
    
    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        ontology: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None
    ) -> List[str]:
        """
        分批提取实体关系并写入Neo4j
        
        Returns:
            episode UUID列表（兼容旧接口）
        """
        import concurrent.futures
        import threading
        
        episode_uuids = []
        total_chunks = len(chunks)
        write_lock = threading.Lock()
        
        # 获取本体（如果没有传入，从Neo4j读取）
        if ontology is None:
            ontology = self._get_ontology(graph_id)
            
        def process_chunk(kwargs) -> Optional[str]:
            i = kwargs['index']
            chunk = kwargs['chunk']
            
            # 使用LLM提取实体和关系
            extraction = self.extractor.extract(
                text=chunk,
                ontology=ontology,
                chunk_index=i,
                total_chunks=total_chunks
            )
            
            # 使用锁进行顺序写入，避免Neo4j并发写入死锁
            with write_lock:
                ep_uuid = self._write_extraction_to_neo4j(graph_id, extraction)
            return ep_uuid

        completed_count = 0
        
        # Use ThreadPool with limited workers to avoid starving Flask server
        max_workers = 2 if threading.active_count() > 15 else 4
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 提交所有任务
            futures = []
            for i, chunk in enumerate(chunks):
                futures.append(executor.submit(process_chunk, {'index': i, 'chunk': chunk}))
                
            for future in concurrent.futures.as_completed(futures):
                completed_count += 1
                if progress_callback:
                    progress = completed_count / total_chunks
                    progress_callback(
                        f"处理第 {completed_count}/{total_chunks} 个文本块...",
                        progress
                    )
                try:
                    ep_uuid = future.result()
                    if ep_uuid:
                        episode_uuids.append(ep_uuid)
                except Exception as e:
                    logger.error(f"处理实体提取时发生错误: {str(e)}")
                
                # Yield CPU to Flask request threads
                time.sleep(0.1)
                
        logger.info(f"共处理 {total_chunks} 个文本块, 写入 {len(episode_uuids)} 批数据")
        return episode_uuids
    
    def _get_ontology(self, graph_id: str) -> Dict[str, Any]:
        """从Neo4j获取图谱的本体定义"""
        import json
        with self.driver.session() as session:
            result = session.run(
                "MATCH (g:GraphMeta {graph_id: $graph_id}) RETURN g.ontology AS ontology",
                graph_id=graph_id
            )
            record = result.single()
            if record and record["ontology"]:
                return json.loads(record["ontology"])
        return {"entity_types": [], "edge_types": []}
    
    def _write_extraction_to_neo4j(
        self,
        graph_id: str,
        extraction: Dict[str, Any]
    ) -> Optional[str]:
        """将提取结果写入Neo4j"""
        import json
        entities = extraction.get("entities", [])
        relationships = extraction.get("relationships", [])
        
        if not entities and not relationships:
            return None
        
        ep_uuid = str(uuid.uuid4())
        
        with self.driver.session() as session:
            # 写入实体节点（Batch UNWIND）
            valid_entities = []
            for entity in entities:
                entity_name = entity.get("name", "")
                entity_type = entity.get("entity_type", "Entity")
                summary = entity.get("summary", "")
                attributes = entity.get("attributes", {})
                
                if not entity_name:
                    continue
                
                valid_entities.append({
                    "name": entity_name,
                    "safe_label": _safe_label(entity_type),
                    "summary": summary,
                    "attributes_json": json.dumps(attributes, ensure_ascii=False),
                    "uuid": str(uuid.uuid4())
                })
            
            entities_by_label = {}
            for e in valid_entities:
                lbl = e['safe_label']
                if lbl not in entities_by_label:
                    entities_by_label[lbl] = []
                entities_by_label[lbl].append(e)
                
            for label, ents in entities_by_label.items():
                session.run(
                    f"""
                    UNWIND $entities AS ent
                    MERGE (n:Entity:{label} {{name: ent.name, graph_id: $graph_id}})
                    ON CREATE SET
                        n.uuid = ent.uuid,
                        n.summary = ent.summary,
                        n.attributes = ent.attributes_json,
                        n.created_at = datetime()
                    ON MATCH SET
                        n.summary = CASE WHEN size(n.summary) < size(ent.summary) THEN ent.summary ELSE n.summary END,
                        n.attributes = ent.attributes_json
                    """,
                    entities=ents,
                    graph_id=graph_id
                )
            
            # 写入关系 (Batch UNWIND)
            valid_rels = []
            for rel in relationships:
                source_name = rel.get("source", "")
                target_name = rel.get("target", "")
                rel_type = rel.get("relation_type", "RELATED_TO")
                fact = rel.get("fact", "")
                
                if not source_name or not target_name:
                    continue
                
                valid_rels.append({
                    "source": source_name,
                    "target": target_name,
                    "rel_type": rel_type,
                    "safe_rel": _safe_rel_type(rel_type),
                    "fact": fact,
                    "uuid": str(uuid.uuid4())
                })
                
            rels_by_type = {}
            for r in valid_rels:
                rtype = r['safe_rel']
                if rtype not in rels_by_type:
                    rels_by_type[rtype] = []
                rels_by_type[rtype].append(r)
                
            for rtype, rels_batch in rels_by_type.items():
                session.run(
                    f"""
                    UNWIND $rels AS rel
                    MATCH (s:Entity {{name: rel.source, graph_id: $graph_id}})
                    MATCH (t:Entity {{name: rel.target, graph_id: $graph_id}})
                    MERGE (s)-[r:{rtype} {{graph_id: $graph_id}}]->(t)
                    ON CREATE SET
                        r.uuid = rel.uuid,
                        r.fact = rel.fact,
                        r.name = rel.rel_type,
                        r.created_at = datetime()
                    ON MATCH SET
                        r.fact = rel.fact
                    """,
                    rels=rels_batch,
                    graph_id=graph_id
                )
        
        return ep_uuid
    
    def _wait_for_episodes(
        self,
        episode_uuids: List[str],
        progress_callback: Optional[Callable] = None,
        timeout: int = 600
    ):
        """
        兼容旧接口 - Neo4j是同步写入的，不需要等待
        """
        if progress_callback:
            progress_callback("Neo4j写入已完成（同步操作）", 1.0)
    
    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """获取图谱信息"""
        with self.driver.session() as session:
            # 获取节点数和实体类型
            result = session.run(
                """
                MATCH (n:Entity {graph_id: $graph_id})
                RETURN count(n) AS node_count, collect(DISTINCT labels(n)) AS all_labels
                """,
                graph_id=graph_id
            )
            record = result.single()
            node_count = record["node_count"] if record else 0
            
            # 提取实体类型（排除Entity和Node标签）
            entity_types = set()
            if record and record["all_labels"]:
                for label_list in record["all_labels"]:
                    for label in label_list:
                        if label not in ["Entity", "Node"]:
                            entity_types.add(label)
            
            # 获取边数
            result = session.run(
                """
                MATCH (s:Entity {graph_id: $graph_id})-[r]->(t:Entity {graph_id: $graph_id})
                WHERE NOT type(r) IN ['CONTAINS']
                RETURN count(r) AS edge_count
                """,
                graph_id=graph_id
            )
            record = result.single()
            edge_count = record["edge_count"] if record else 0
        
        return GraphInfo(
            graph_id=graph_id,
            node_count=node_count,
            edge_count=edge_count,
            entity_types=list(entity_types)
        )
    
    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """
        获取完整图谱数据（包含详细信息）
        
        返回格式与旧Zep版本完全兼容
        """
        with self.driver.session() as session:
            # 获取所有节点
            result = session.run(
                """
                MATCH (n:Entity {graph_id: $graph_id})
                RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels,
                       n.summary AS summary, n.attributes AS attributes,
                       toString(n.created_at) AS created_at
                ORDER BY n.name
                """,
                graph_id=graph_id
            )
            
            nodes_data = []
            node_map = {}
            for record in result:
                node_uuid = record["uuid"] or ""
                node_name = record["name"] or ""
                node_map[node_uuid] = node_name
                # Also map by name for edges
                node_map[node_name] = node_name
                
                nodes_data.append({
                    "uuid": node_uuid,
                    "name": node_name,
                    "labels": record["labels"] or [],
                    "summary": record["summary"] or "",
                    "attributes": record["attributes"] or {},
                    "created_at": record["created_at"],
                })
            
            # 获取所有边
            result = session.run(
                """
                MATCH (s:Entity {graph_id: $graph_id})-[r]->(t:Entity {graph_id: $graph_id})
                WHERE NOT type(r) IN ['CONTAINS']
                RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact,
                       type(r) AS fact_type,
                       s.uuid AS source_node_uuid, t.uuid AS target_node_uuid,
                       s.name AS source_node_name, t.name AS target_node_name,
                       r.attributes AS attributes,
                       toString(r.created_at) AS created_at
                """,
                graph_id=graph_id
            )
            
            edges_data = []
            for record in result:
                edges_data.append({
                    "uuid": record["uuid"] or "",
                    "name": record["name"] or "",
                    "fact": record["fact"] or "",
                    "fact_type": record["fact_type"] or "",
                    "source_node_uuid": record["source_node_uuid"] or "",
                    "target_node_uuid": record["target_node_uuid"] or "",
                    "source_node_name": record["source_node_name"] or "",
                    "target_node_name": record["target_node_name"] or "",
                    "attributes": record["attributes"] or {},
                    "created_at": record["created_at"],
                    "episodes": [],
                })
        
        return {
            "graph_id": graph_id,
            "nodes": nodes_data,
            "edges": edges_data,
            "node_count": len(nodes_data),
            "edge_count": len(edges_data),
        }
    
    def delete_graph(self, graph_id: str):
        """删除图谱及其所有节点和关系"""
        with self.driver.session() as session:
            # 删除所有属于该图谱的节点和关系
            session.run(
                """
                MATCH (n {graph_id: $graph_id})
                DETACH DELETE n
                """,
                graph_id=graph_id
            )
            # 删除GraphMeta节点
            session.run(
                "MATCH (g:GraphMeta {graph_id: $graph_id}) DELETE g",
                graph_id=graph_id
            )
        logger.info(f"Neo4j图谱已删除: {graph_id}")


def _safe_label(label: str) -> str:
    """确保Neo4j标签名是安全的（只包含字母数字和下划线）"""
    import re
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', label)
    if not safe or not safe[0].isalpha():
        safe = 'L_' + safe
    return safe


def _safe_rel_type(rel_type: str) -> str:
    """确保Neo4j关系类型名称是安全的"""
    import re
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', rel_type)
    if not safe or not safe[0].isalpha():
        safe = 'R_' + safe
    return safe.upper()
