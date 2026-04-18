#!/bin/bash
python -m uvicorn app.portal.physician_portal_app:app --host 0.0.0.0 --port $PORT
