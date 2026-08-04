from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.db import SessionLocal, create_schema
from app.services.processor import JobProcessor
from app.services.queue import claim_next_job, schedule_due_accounts

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("threads-monitor.worker")


def run() -> None:
    create_schema()
    processor = JobProcessor(settings)
    logger.info("背景 Worker 已啟動")
    while True:
        try:
            with SessionLocal() as db:
                schedule_due_accounts(db, settings)
                db.commit()
                job = claim_next_job(db, settings)
                if not job:
                    db.commit()
                    time.sleep(15)
                    continue
                db.commit()
                logger.info("執行工作 id=%s kind=%s account=%s", job.id, job.kind, job.account_id)
                processor.process(db, job)
                db.commit()
        except KeyboardInterrupt:
            logger.info("背景 Worker 已停止")
            return
        except Exception:
            logger.exception("背景 Worker 迴圈發生錯誤")
            time.sleep(15)


if __name__ == "__main__":
    run()
