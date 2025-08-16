from celery import shared_task

@shared_task(name='app.tasks.video_tasks.add')
def add(x, y):
    """简单的加法任务 - 用于测试Celery连接"""
    print(f"执行任务: {x} + {y}")
    return x + y