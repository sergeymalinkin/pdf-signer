"""Durable MAX approval workflow. Only configured approver may authorize signing."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from max_api import ApiError

ACTIVE=('CREATED','NOTIFYING','WAITING','PROCESSING','SENDING')


def pdfs(message):
    return [a for a in (message.get('body') or {}).get('attachments',[]) or []
            if a.get('type')=='file' and str(a.get('filename','')).lower().endswith('.pdf')]


def attachment_identity(attachment):
    payload=attachment.get('payload') or {}
    # MAX refreshes both URL and token on reads; fileId is stable.
    token=payload.get('fileId') or payload.get('token')
    if isinstance(token,int): token=str(token)
    if not isinstance(token,str) or not token: raise ValueError('Attachment has no stable file token')
    return hashlib.sha256((token+'\0'+attachment['filename']+'\0'+str(attachment.get('size'))).encode()).hexdigest()


def run_signer(source, config, output):
    result=subprocess.run([sys.executable,str(Path(__file__).with_name('signer.py')),str(source),
                           '--config',str(config),'--output',str(output),'--max-pages','40'],
                          capture_output=True,timeout=120)
    if result.returncode not in (0,2): raise RuntimeError('PDF processing failed')
    target=output/(source.stem+'_SIGNED.pdf')
    rows=json.loads(target.with_suffix('.json').read_text(encoding='utf-8'))['pages']
    if not rows: raise RuntimeError('No pages were processed')
    return target,rows


def display_name(user):
    name=' '.join(str(user.get(k) or '').strip() for k in ('first_name','last_name')).strip()
    name=name or str(user.get('name') or '').strip() or str(user.get('username') or '').strip()
    return name[:80] or None


def approver_label(name,uid):
    return f'{name} (ID {uid})' if name else f'ID {uid}'


class Store:
    def __init__(self, path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,payload TEXT,state TEXT NOT NULL,created REAL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,chat INTEGER,mid TEXT,attachment TEXT,filename TEXT,
                state TEXT,notification_mid TEXT,source_sha TEXT,result_mid TEXT,reason TEXT,created REAL,approver INTEGER,
                UNIQUE(chat,mid,attachment));
            CREATE TABLE IF NOT EXISTS deliveries(chat INTEGER,sha TEXT,job TEXT,PRIMARY KEY(chat,sha));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS notifications(job TEXT,user INTEGER,mid TEXT,PRIMARY KEY(job,user));
            ''')
            columns={row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            for name,kind in (('approver','INTEGER'),('approved_at','REAL'),('approver_name','TEXT')):
                if name not in columns: db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        return db
    def enqueue(self,event):
        raw=json.dumps(event,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        key=hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',(key,raw,'NEW',time.time()))
    def job(self,jid):
        with self.connect() as db: return db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
    def state(self,jid,state,reason=None,**values):
        fields=dict(values,state=state,reason=reason)
        with self.connect() as db:
            db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),jid))
    def claim(self,jid):
        with self.connect() as db:
            return db.execute("UPDATE jobs SET state='PROCESSING' WHERE id=? AND state='WAITING'",(jid,)).rowcount==1
    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE events SET state='ERROR',payload=NULL WHERE state='BUSY'")
            db.execute("UPDATE jobs SET state='DELIVERY_UNKNOWN',reason='Restart during external operation' WHERE state IN ('NOTIFYING','SENDING')")
            db.execute("UPDATE jobs SET state='ERROR',reason='Restart during processing' WHERE state IN ('CREATED','PROCESSING')")


class Workflow:
    def __init__(self, store, api, chat_ids, approver_id, config, workdir, processor=run_signer, chat_names=None, approvers=None):
        self.store,self.api,self.chat_ids,self.approver_id=store,api,set(chat_ids),approver_id
        self.config,self.workdir,self.processor=Path(config),Path(workdir).resolve(),processor
        self.chat_names=chat_names or {chat:'Рабочий чат '+str(i+1) for i,chat in enumerate(chat_ids)}
        self.workdir.mkdir(parents=True,exist_ok=True)
        self.approvers={chat:set((approvers or {}).get(str(chat),[approver_id])) for chat in chat_ids}
        if any(not users or any(type(uid)!=int or uid<=0 for uid in users) for users in self.approvers.values()):
            raise ValueError('Invalid approver configuration')
        self.private_chat=None
        self.pressed=None
    def private(self,text,attachments=None):
        users=self.approvers.get(self.private_chat,{self.approver_id})
        if self.pressed: text+='\n'+self.pressed
        response=None
        for uid in sorted(users):
            try: response=self.api.send(user_id=uid,text=text,attachments=attachments)
            except Exception: print('Private notification failed for one configured approver',flush=True)
        if response is None: raise RuntimeError('No approver notified')
        return response
    def handle(self,event):
        kind=event.get('update_type')
        if kind=='bot_started' and (event.get('user') or {}).get('user_id') in set().union(*self.approvers.values()):
            self.api.send(user_id=event['user']['user_id'],text='Вы подключены как ответственный. Заявки из доступных рабочих чатов будут приходить сюда с кнопкой «Подписать».')
        elif kind=='bot_started':
            uid=(event.get('user') or {}).get('user_id')
            if isinstance(uid,int):
                self.api.send(user_id=uid,text=f'Ваш MAX ID: {uid}. Передайте его администратору для настройки доступа. Доступ к заявкам пока не выдан.')
        elif kind=='message_created' and (event.get('message') or {}).get('recipient',{}).get('chat_type')=='dialog' and str((event.get('message') or {}).get('body',{}).get('text','')).strip()=='/id':
            uid=(event['message'].get('sender') or {}).get('user_id')
            if isinstance(uid,int): self.api.send(user_id=uid,text=f'Ваш MAX ID: {uid}')
        elif kind=='bot_added' and event.get('chat_id') in self.chat_ids:
            chat=event['chat_id']
            self.chat_names[chat]=self.api.request('GET',f'/chats/{chat}').get('title',self.chat_names[chat])
        elif kind=='message_callback': self.callback(event)
        elif kind=='message_created': self.incoming(event.get('message') or {})
        elif kind in ('message_edited','message_removed'):
            message=event.get('message') or {}
            mid=event.get('message_id') or (message.get('body') or {}).get('mid')
            chat=event.get('chat_id') or (message.get('recipient') or {}).get('chat_id')
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET state='CANCELLED',reason='Source message changed or removed' WHERE chat=? AND mid=? AND state IN ('WAITING','CREATED')",(chat,mid))
            if kind=='message_edited': self.incoming(message)
    def incoming(self,message):
        chat_id=(message.get('recipient') or {}).get('chat_id')
        if chat_id not in self.chat_ids: return
        self.private_chat=chat_id
        if (message.get('sender') or {}).get('is_bot'): return
        files=pdfs(message)
        if not files: return
        mid=(message.get('body') or {}).get('mid')
        if not mid: raise ValueError('Message has no ID')
        if len(files)!=1:
            self.private('В сообщении несколько PDF. Для пилота отправьте каждую заявку отдельным сообщением.')
            return
        attachment=files[0]; identity=attachment_identity(attachment)
        if int(attachment.get('size') or 0)>20*1024*1024:
            self.private('Заявка больше 20 МБ. Нужна ручная проверка.'); return
        jid=uuid.uuid4().hex
        with self.store.connect() as db:
            inserted=db.execute('INSERT OR IGNORE INTO jobs(id,chat,mid,attachment,filename,state,created) VALUES(?,?,?,?,?,?,?)',
                                (jid,chat_id,mid,identity,attachment['filename'],'CREATED',time.time())).rowcount
        if not inserted: return
        try:
            folder=self.workdir/jid; folder.mkdir()
            source=folder/'request.pdf'
            self.api.download(attachment['payload']['url'],source,20*1024*1024)
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            self.store.state(jid,'NOTIFYING',source_sha=digest)
            keyboard=[{'type':'inline_keyboard','payload':{'buttons':[[
                {'type':'callback','text':'Подписать','payload':'sign:'+jid},
                {'type':'callback','text':'Пропустить','payload':'skip:'+jid}]]}}]
            notification=None
            last_error=None
            for uid in sorted(self.approvers[chat_id]):
                try:
                    response=self.api.send(user_id=uid,text='Новая PDF-заявка: '+attachment['filename'][:160]+'\nЧат: '+self.chat_names[chat_id]+'\nПосле проверки результат вернётся в этот чат.',attachments=keyboard)
                    current=response['message']['body']['mid']
                    with self.store.connect() as db:
                        db.execute('INSERT INTO notifications VALUES(?,?,?)',(jid,uid,current))
                    if uid==self.approver_id or notification is None: notification=current
                except Exception as exc:
                    last_error=exc
                    print('Approval notification failed for one configured approver',flush=True)
            if notification is None: raise last_error or RuntimeError('No approver notified')
            self.store.state(jid,'WAITING',notification_mid=notification)
        except Exception as exc:
            state='NOTIFICATION_UNKNOWN' if self.store.job(jid)['state']=='NOTIFYING' else 'ERROR'
            self.store.state(jid,state,type(exc).__name__)
            raise
    def answer(self,callback,text):
        try: self.api.answer(callback['callback_id'],text)
        except Exception: pass  # Callback popup is advisory; never gates durable processing.
    def source_matches(self,job,message):
        files=pdfs(message)
        if (message.get('recipient') or {}).get('chat_id')!=job['chat'] or len(files)!=1:
            return False
        if attachment_identity(files[0])!=job['attachment']: return False
        check=self.workdir/job['id']/'check.pdf'
        try:
            self.api.download(files[0]['payload']['url'],check,20*1024*1024)
            return hashlib.sha256(check.read_bytes()).hexdigest()==job['source_sha']
        finally: check.unlink(missing_ok=True)
    def callback(self,event):
        callback=event.get('callback') or {}
        user=callback.get('user') or {}
        uid=user.get('user_id')
        name=display_name(user)
        action,separator,jid=str(callback.get('payload','')).partition(':')
        if not separator or action not in ('sign','skip'): return
        job=self.store.job(jid)
        if not job or uid not in self.approvers.get(job['chat'],set()):
            self.answer(callback,'Подписание доступно только ответственному этого чата.'); return
        self.private_chat=job['chat']
        message=event.get('message') or {}
        with self.store.connect() as db:
            notice=db.execute('SELECT mid FROM notifications WHERE job=? AND user=?',(jid,uid)).fetchone()
        expected_mid=notice['mid'] if notice else job['notification_mid'] if uid==self.approver_id else None
        if not expected_mid or (message.get('body') or {}).get('mid') != expected_mid:
            self.answer(callback,'Кнопка не относится к этой заявке.'); return
        if job['state']!='WAITING':
            status={'SENT':'Уже подписано: '+approver_label(job['approver_name'],job['approver']), 'PROCESSING':'Заявка уже обрабатывается.', 'SENDING':'Заявка уже отправляется.', 'ALREADY_SIGNED':'Все страницы уже подписаны.'}.get(job['state'],'Заявка уже обработана или недоступна.')
            self.answer(callback,status); return
        if time.time()-job['created']>86400:
            self.store.state(jid,'EXPIRED','Approval expired'); self.clean(jid)
            self.answer(callback,'Срок обработки истёк. Отправьте заявку заново.'); return
        if action=='skip':
            self.store.state(jid,'SKIPPED','Skipped by authorized approver',approver=uid,approved_at=time.time(),approver_name=name); self.clean(jid)
            self.answer(callback,'Заявка пропущена.'); return
        if not self.store.claim(jid): return
        self.store.state(jid,'PROCESSING',approver=uid,approved_at=time.time(),approver_name=name)
        self.answer(callback,'Проверяю заявку…')
        self.pressed='Файл: '+job['filename'][:160]+'\nНажал: '+approver_label(name,uid)
        try:
            original=self.api.message(job['mid'])
            if not self.source_matches(job,original):
                self.store.state(jid,'CANCELLED','Source changed before approval')
                self.private('Заявка изменена. Старое подтверждение отменено.'); return
            source=self.workdir/jid/'request.pdf'
            if hashlib.sha256(source.read_bytes()).hexdigest()!=job['source_sha']: raise RuntimeError('Source integrity check failed')
            output,rows=self.processor(source,self.config,self.workdir/jid/'result')
            if any(r['status'] not in ('SIGNED','ALREADY_SIGNED') for r in rows):
                self.store.state(jid,'REVIEW_REQUIRED','One or more pages rejected')
                explanations={
                    'Existing or ambiguous marks in carrier signature area; no overlay':'В области подписи уже есть отметки. Это может быть существующая подпись или печать; новую поверх них не ставлю.',
                    'Signature anchors do not match verified layout':'Расположение блока подписи не соответствует проверенному шаблону.',
                    'Page geometry is outside verified profile':'Размер или поворот страницы не соответствует проверенному шаблону.',
                }
                reasons='\n'.join(f"Страница {r['page']}: {explanations.get(r['reason'],r['reason'])}" for r in rows if r['status'] not in ('SIGNED','ALREADY_SIGNED'))
                self.private('Заявка не опубликована: нужна проверка.\n'+reasons[:3500]); return
            if all(r['status']=='ALREADY_SIGNED' for r in rows):
                self.store.state(jid,'ALREADY_SIGNED','No new signatures needed')
                self.private('Заявка уже подписана. Повторно в общий чат её не отправляю.'); return
            added=sum(r['status']=='SIGNED' for r in rows)
            preserved=sum(r['status']=='ALREADY_SIGNED' for r in rows)
            with self.store.connect() as db:
                if not db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',(job['chat'],job['source_sha'],jid)).rowcount:
                    duplicate=True
                else: duplicate=False
            if duplicate:
                self.store.state(jid,'DUPLICATE','Document already claimed for publication')
                self.private('Этот PDF уже обрабатывался. Повторной публикации не будет.'); return
            upload_name=Path(job['filename'].replace('\\','/')).stem+'_SIGNED.pdf'
            token=self.api.upload(output,filename=upload_name)
            # Recheck source immediately before publication.
            latest=self.api.message(job['mid'])
            if not self.source_matches(job,latest):
                self.store.state(jid,'CANCELLED','Source changed before publication'); return
            self.store.state(jid,'SENDING')
            summary=f'Проверены все страницы: {len(rows)}. Подписано: {added}. Уже подписаны и сохранены: {preserved}.'
            sent=self.api.send(chat_id=job['chat'],reply_to=job['mid'],text='Заявка подписана. '+summary,
                               attachments=[{'type':'file','payload':{'token':token}}])
            self.store.state(jid,'SENT',result_mid=sent['message']['body']['mid'])
            self.private('Готово. Подписанная заявка отправлена в рабочий чат.\n'+summary)
        except Exception as exc:
            state=self.store.job(jid)['state']
            if state=='SENT': return
            uncertain=state=='SENDING'
            self.store.state(jid,'DELIVERY_UNKNOWN' if uncertain else 'ERROR',type(exc).__name__)
            try: self.private('Не удалось подтвердить доставку. Проверьте рабочий чат; автоматического повтора не будет.' if uncertain else 'Обработка остановлена из-за ошибки. Заявка не опубликована.')
            except Exception: pass
        finally:
            self.pressed=None
            self.clean(jid)
    def clean(self,jid):
        # Only generated files under this exact job directory, no recursive path traversal.
        if len(jid)!=32 or any(c not in '0123456789abcdef' for c in jid): raise ValueError('Invalid job path')
        folder=self.workdir/jid
        if not folder.exists(): return
        for path in (folder/'request.pdf',folder/'result/request_SIGNED.pdf',folder/'result/request_SIGNED.pdf.tmp'):
            path.unlink(missing_ok=True)
    def expire(self):
        with self.store.connect() as db:
            expired=db.execute("SELECT id FROM jobs WHERE created<? AND state IN ('WAITING','ERROR','CANCELLED','EXPIRED','NOTIFICATION_UNKNOWN','DELIVERY_UNKNOWN')",(time.time()-86400,)).fetchall()
            db.execute("UPDATE jobs SET state='EXPIRED',reason='Approval expired' WHERE created<? AND state='WAITING'",(time.time()-86400,))
        for row in expired: self.clean(row['id'])
    def tick(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM events WHERE state='NEW' ORDER BY created LIMIT 1").fetchone()
            if not row: return False
            db.execute("UPDATE events SET state='BUSY' WHERE id=?",(row['id'],))
        try:
            self.handle(json.loads(row['payload']))
            state='DONE'
        except Exception:
            state='ERROR'
        with self.store.connect() as db:
            db.execute('UPDATE events SET state=?,payload=NULL WHERE id=?',(state,row['id']))
        if state=='ERROR': print('Event failed; inspect local job states',flush=True)
        return True
