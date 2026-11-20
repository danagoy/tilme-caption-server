# tilme-caption-server

Experimental Python transcript API for tilme Shadowing. 154-video library is stored in the Android app and is not modified by this repository.

### Render Free deployment
- Runtime: Python 3.12.12
- Build: `pip install -r requirements.txt`
- Start: `python tilme_server.py --host 0.0.0.0 --port $PORT`
- Health: `/health`
- Plan: Free (see render.yaml). No payment method recommended.

> Note: `youtube-transcript-api` is unofficial. YouTube may block Render's IP; a live health endpoint does not guarantee transcripts. Respect owners' rights and platform policies.
