"""Webhook entrypoint. Bind to localhost behind an HTTPS reverse proxy."""
import argparse
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from max_api import MaxAPI, ApiError
from max_workflow import Store, Workflow

ROOT=Path(__file__).resolve().parent
UPDATE_TYPES=['bot_started','bot_added','message_created','message_callback','message_edited','message_removed']


def load_env(path):
    if not path.exists(): return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'): continue
        name,separator,value=line.partition('=')
        if separator and name.strip().startswith(('MAX_','SIGNER_')):
            os.environ.setdefault(name.strip(),value.strip().strip('"').strip("'"))


def authorized(provided, expected):
    return bool(expected) and hmac.compare_digest(provided.encode(),expected.encode())


def handler_for(store, secret):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            if self.path=='/healthz':
                self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
            else: self.send_error(404)
        def do_POST(self):
            self.connection.settimeout(10)
            if self.path!='/max/webhook': self.send_error(404); return
            if not authorized(self.headers.get('X-Max-Bot-Api-Secret',''),secret):
                self.send_error(403); return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1024*1024: self.send_error(413); return
                event=json.loads(self.rfile.read(length))
                if not isinstance(event,dict): raise ValueError()
                if event.get('update_type') not in UPDATE_TYPES:
                    self.send_response(200); self.end_headers(); return
                store.enqueue(event)  # Durable inbox before acknowledging MAX.
            except (ValueError,UnicodeError): self.send_error(400); return
            except Exception: self.send_error(503); return
            self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
    return Handler


def settings():
    ids=[int(v.strip()) for v in os.environ.get('MAX_CHAT_IDS','').split(',') if v.strip()]
    approver=int(os.environ.get('MAX_APPROVER_ID','33162943'))
    if not ids: raise ValueError('MAX_CHAT_IDS is required')
    secret=os.environ.get('MAX_WEBHOOK_SECRET','')
    if len(secret)<24: raise ValueError('MAX_WEBHOOK_SECRET must have at least 24 characters')
    return ids,approver,secret


def worker(workflow, stop):
    while not stop.is_set():
        try:
            if not workflow.tick(): stop.wait(.25)
            workflow.expire()
        except Exception:
            print('Worker error; local state requires inspection',flush=True)
            stop.wait(2)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',type=Path,default=ROOT/'private/max.env')
    parser.add_argument('--register-webhook',action='store_true')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    load_env(args.env)
    ids,approver,secret=settings()
    api=MaxAPI(os.environ.get('MAX_BOT_TOKEN'),os.environ.get('MAX_CA_FILE'))
    if args.check:
        info=api.request('GET','/me')
        print('MAX API connected. Bot ID:',info['user_id'])
        chats=api.request('GET','/chats')
        known={c['chat_id'] for c in chats.get('chats',[])}
        for chat in ids:
            if chat not in known: raise ValueError('Configured chat is not available to this bot')
            me=api.request('GET',f'/chats/{chat}/members/me')
            if not me.get('is_admin'): raise ValueError('Bot needs admin rights in configured chat')
        print('Configured chats and admin rights verified. Approver ID:',approver)
        return
    if args.register_webhook:
        url=os.environ.get('MAX_WEBHOOK_URL','')
        if urlsplit(url).scheme!='https' or not url.endswith('/max/webhook'):
            raise ValueError('HTTPS MAX_WEBHOOK_URL ending in /max/webhook is required')
        api.request('POST','/subscriptions',body={'url':url,'secret':secret,'update_types':UPDATE_TYPES})
        print('Webhook registered')
        return
    config=Path(os.environ.get('SIGNER_CONFIG',str(ROOT/'config.local.json')))
    data=Path(os.environ.get('SIGNER_DATA',str(ROOT/'private/max-state')))
    store=Store(data/'state.sqlite3'); store.recover()
    names={}
    for index,chat in enumerate(ids):
        try: names[chat]=api.request('GET',f'/chats/{chat}').get('title','Рабочий чат')
        except ApiError as exc:
            if exc.status not in (403,404): raise
            names[chat]='Рабочий чат '+str(index+1)
    approvers=json.loads(os.environ.get('MAX_APPROVERS_JSON','{}'))
    workflow=Workflow(store,api,ids,approver,config,data/'jobs',chat_names=names,approvers=approvers)
    stop=threading.Event()
    thread=threading.Thread(target=worker,args=(workflow,stop),daemon=True); thread.start()
    server=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('MAX_PORT','8098'))),handler_for(store,secret))
    server.timeout=2
    print('MAX signer started; waiting for webhook events',flush=True)
    try: server.serve_forever()
    finally: stop.set(); server.server_close(); thread.join(timeout=5)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        # Never print raw API responses, token, signed URL, or document bytes.
        raise SystemExit('Startup failed: '+type(exc).__name__)
