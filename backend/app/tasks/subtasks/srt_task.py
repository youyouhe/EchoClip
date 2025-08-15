from celery import shared_task
from app.core.celery import celery_app  # 确保Celery应用被初始化
import asyncio
import tempfile
import os
import requests
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any
from app.services.audio_processor import audio_processor
from app.services.minio_client import minio_service
from app.services.state_manager import get_state_manager
from app.core.constants import ProcessingTaskType, ProcessingTaskStatus, ProcessingStage
from app.core.database import get_sync_db
from app.core.config import settings
from app.models import Video, ProcessingTask
from sqlalchemy import select

# 创建logger
logger = logging.getLogger(__name__)

@shared_task(bind=True)
def generate_srt(self, video_id: str, project_id: int, user_id: int, split_files: list = None, create_processing_task: bool = True) -> Dict[str, Any]:
    """Generate SRT subtitles from audio using ASR"""
    
    def _ensure_processing_task_exists(celery_task_id: str, video_id: int) -> bool:
        """确保处理任务记录存在"""
        try:
            with get_sync_db() as db:
                state_manager = get_state_manager(db)
                
                # 检查任务是否已存在
                task = db.query(ProcessingTask).filter(
                    ProcessingTask.celery_task_id == celery_task_id
                ).first()
                
                if not task:
                    # 创建新的处理任务记录
                    task = ProcessingTask(
                        video_id=int(video_id),
                        task_type=ProcessingTaskType.GENERATE_SRT,
                        task_name="字幕生成",
                        celery_task_id=celery_task_id,
                        input_data={"direct_audio": True},
                        status=ProcessingTaskStatus.RUNNING,
                        started_at=datetime.utcnow(),
                        progress=0.0,
                        stage=ProcessingStage.GENERATE_SRT
                    )
                    db.add(task)
                    db.commit()
                    print(f"Created new processing task for celery_task_id: {celery_task_id}")
                    return True
                return True
        except Exception as e:
            print(f"Error ensuring processing task exists: {e}")
            return False
    
    def _get_audio_file_from_db(video_id_str: str) -> dict:
        """从数据库获取音频文件信息 - 同步版本"""
        with get_sync_db() as db:
            from sqlalchemy import select
            from app.models.processing_task import ProcessingTask
            from app.models.video import Video
            
            # 首先查找视频记录
            video = db.query(Video).filter(Video.id == int(video_id_str)).first()
            if not video:
                return None
                
            # 尝试从视频的processing_metadata中获取音频路径
            audio_path = None
            if video.processing_metadata and video.processing_metadata.get('audio_path'):
                audio_path = video.processing_metadata.get('audio_path')
            else:
                # 查找最新的成功完成的extract_audio任务
                stmt = select(ProcessingTask).where(
                    ProcessingTask.video_id == int(video_id_str),
                    ProcessingTask.task_type == ProcessingTaskType.EXTRACT_AUDIO,
                    ProcessingTask.status == ProcessingTaskStatus.SUCCESS
                ).order_by(ProcessingTask.completed_at.desc())
                
                result = db.execute(stmt)
                task = result.first()
                
                if task and task[0].output_data:
                    audio_path = task[0].output_data.get('minio_path')
            
            if audio_path:
                return {"audio_path": audio_path, "video_id": video_id_str}
            return None
    
    def _update_task_status(celery_task_id: str, status: str, progress: float, message: str = None, error: str = None):
        """更新任务状态 - 同步版本"""
        # 如果这个任务是作为子任务运行的，不创建处理任务记录
        if not create_processing_task:
            print(f"Skipping status update for sub-task {celery_task_id} (create_processing_task=False)")
            return
            
        try:
            # 确保任务存在
            _ensure_processing_task_exists(celery_task_id, video_id)
            
            with get_sync_db() as db:
                state_manager = get_state_manager(db)
                state_manager.update_celery_task_status_sync(
                    celery_task_id=celery_task_id,
                    celery_status=status,
                    meta={
                        'progress': progress,
                        'message': message,
                        'error': error,
                        'stage': ProcessingStage.GENERATE_SRT
                    }
                )
        except ValueError as e:
            # 记录详细错误信息
            print(f"Warning: Processing task update failed - {type(e).__name__}: {e}")
        except Exception as e:
            print(f"Error updating task status: {type(e).__name__}: {e}")
    
    def run_async(coro):
        """运行异步代码的辅助函数"""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    
    try:
        celery_task_id = self.request.id
        if not celery_task_id:
            celery_task_id = "unknown"
            
        _update_task_status(celery_task_id, ProcessingTaskStatus.RUNNING, 10, "开始生成字幕")
        self.update_state(state='PROGRESS', meta={'progress': 10, 'stage': ProcessingStage.GENERATE_SRT, 'message': '开始生成字幕'})
        
        # 获取音频文件信息
        audio_info = _get_audio_file_from_db(video_id)
        if not audio_info:
            error_msg = "没有找到可用的音频文件，请先提取音频"
            _update_task_status(celery_task_id, ProcessingTaskStatus.FAILURE, 0, error_msg)
            raise Exception(error_msg)
        
        with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                audio_filename = f"{video_id}.wav"
                audio_path = temp_path / audio_filename
                
                _update_task_status(celery_task_id, ProcessingTaskStatus.RUNNING, 30, "正在下载音频文件")
                self.update_state(state='PROGRESS', meta={'progress': 30, 'stage': ProcessingStage.GENERATE_SRT, 'message': '正在下载音频文件'})
                
                # Handle both full URLs and object names
                audio_minio_path = audio_info['audio_path']
                from app.core.config import settings
                bucket_prefix = f"{settings.minio_bucket_name}/"
                if audio_minio_path.startswith(bucket_prefix):
                    object_name = audio_minio_path[len(bucket_prefix):]
                else:
                    # Handle both full URLs and object names
                    if "http" in audio_minio_path:
                        # It's a full URL, extract the object name
                        from urllib.parse import urlparse
                        parsed = urlparse(audio_minio_path)
                        path_parts = parsed.path.strip('/').split('/', 1)
                        if len(path_parts) > 1:
                            object_name = path_parts[1]  # Skip bucket name
                        else:
                            object_name = audio_minio_path
                    else:
                        object_name = audio_minio_path
                
                audio_url = run_async(minio_service.get_file_url(object_name, expiry=3600))
                if not audio_url:
                    raise Exception(f"无法获取音频文件URL: {object_name}")
                
                import requests
                response = requests.get(audio_url, stream=True)
                response.raise_for_status()
                
                with open(audio_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                _update_task_status(celery_task_id, ProcessingTaskStatus.RUNNING, 70, "正在生成字幕")
                self.update_state(state='PROGRESS', meta={'progress': 70, 'stage': ProcessingStage.GENERATE_SRT, 'message': '正在生成字幕'})
            
                result = run_async(
                    audio_processor.generate_srt_from_audio(
                        audio_path=str(audio_path),
                        video_id=video_id,
                        project_id=project_id,
                        user_id=user_id
                    )
                )
                
                if result.get('success'):
                    # 保存SRT生成结果到数据库 - 使用同步版本
                    try:
                        with get_sync_db() as db:
                            state_manager = get_state_manager(db)
                            
                            # 通过celery_task_id找到task_id
                            task = db.query(ProcessingTask).filter(
                                ProcessingTask.celery_task_id == celery_task_id
                            ).first()
                            
                            if task:
                                print(f"找到任务记录: task.id={task.id}, task.celery_task_id={task.celery_task_id}")
                                print(f"开始更新任务状态...")
                                
                                state_manager.update_task_status_sync(
                                    task_id=task.id,
                                    status=ProcessingTaskStatus.SUCCESS,
                                    progress=100,
                                    message="字幕生成完成",
                                    output_data={
                                        'srt_filename': result['srt_filename'],
                                        'minio_path': result['minio_path'],
                                        'object_name': result['object_name'],
                                        'total_segments': result['total_segments'],
                                        'processing_stats': result['processing_stats'],
                                        'asr_params': result['asr_params']
                                    },
                                    stage=ProcessingStage.GENERATE_SRT
                                )
                                print(f"任务状态更新完成")
                            else:
                                print(f"未找到任务记录: celery_task_id={celery_task_id}")
                        
                        _update_task_status(celery_task_id, ProcessingTaskStatus.SUCCESS, 100, "字幕生成完成")
                    except Exception as e:
                        print(f"状态更新失败: {e}")
                        import traceback
                        print(f"详细错误信息: {traceback.format_exc()}")
                    
                    self.update_state(state='SUCCESS', meta={'progress': 100, 'stage': ProcessingStage.GENERATE_SRT, 'message': '字幕生成完成'})
                    return {
                        'status': 'completed',
                        'video_id': video_id,
                        'srt_filename': result['srt_filename'],
                        'minio_path': result['minio_path'],
                        'object_name': result['object_name'],
                        'total_segments': result['total_segments'],
                        'processing_stats': result['processing_stats'],
                        'asr_params': result['asr_params']
                    }
                else:
                    raise Exception(result.get('error', 'Unknown error'))
            
    except Exception as e:
        import traceback
        error_msg = str(e)
        error_type = type(e).__name__
        error_details = traceback.format_exc()
        
        print(f"SRT generation failed - {error_type}: {error_msg}")
        print(f"Full traceback: {error_details}")
        
        try:
            _update_task_status(
                self.request.id, 
                ProcessingTaskStatus.FAILURE, 
                0, 
                f"{error_type}: {error_msg}"
            )
        except Exception as status_error:
            print(f"Failed to update task status: {type(status_error).__name__}: {status_error}")
        
        raise Exception(f"{error_type}: {error_msg}")