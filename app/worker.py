from __future__ import annotations

import logging
import time

from app.config import get_settings
from app.db import SessionLocal, create_schema
from app.services.notifications import NotificationService
from app.services.processor import JobProcessor
from app.services.queue import claim_next_job, recover_interrupted_jobs, schedule_due_accounts

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("threads-monitor.worker")


def deliver_notification() -> None:
    with SessionLocal() as db:
        NotificationService(settings).deliver_next(db)
        db.commit()


def run() -> None:
    create_schema()
    processor = JobProcessor(settings)
    logger.info("背景 Worker 已啟動")
    while True:
        try:
            with SessionLocal() as db:
                # Normally this only does work once after startup.  Keeping the
                # idempotent recovery check in the loop also heals a job whose
                # final status commit was interrupted while the process survived.
                recovered = recover_interrupted_jobs(db)
                db.commit()
                if recovered:
                    logger.warning("已回收 %s 個因 Worker 中斷而遺留的工作", recovered)
                schedule_due_accounts(db, settings)
                db.commit()
                job = claim_next_job(db, settings)
                if not job:
                    db.commit()
                    deliver_notification()
                    time.sleep(15)
                    continue
                db.commit()
                logger.info("執行工作 id=%s kind=%s account=%s", job.id, job.kind, job.account_id)
                processor.process(db, job)
                db.commit()
            deliver_notification()
        except KeyboardInterrupt:
            logger.info("背景 Worker 已停止")
            return
        except Exception:
            logger.exception("背景 Worker 迴圈發生錯誤")
            time.sleep(15)


if __name__ == "__main__":
    run()
