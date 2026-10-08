import copy
import json
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from max_workflow import Store,Workflow,run_signer
from max_bot import authorized,handler_for
from max_api import validate_media_url,ApiError

ROOT=Path(__file__).resolve().parents[1]
CHATS=[-78517753808131,-73164206173671]
APPROVER=33162943

class FakeAPI:
 def __init__(self,source):
  self.source=source; self.sent=[]; self.messages={}; self.answers=[]; self.uploaded=[]
  self.fail_group=False; self.fail_private=False
 def send(self,**kw):
  self.sent.append(kw)
  if kw.get('chat_id') is not None and self.fail_group: raise TimeoutError()
  if kw.get('user_id') is not None and self.fail_private: raise TimeoutError()
  return {'message':{'body':{'mid':'out-'+str(len(self.sent))}}}
 def download(self,url,path,limit): path.write_bytes(self.source.read_bytes())
 def message(self,mid): return copy.deepcopy(self.messages[mid])
 def upload(self,path,filename=None):
  self.uploaded.append((path.read_bytes(),filename)); return 'file-token'
 def answer(self,cid,text): self.answers.append((cid,text))

def message(mid='incoming',chat=CHATS[0],token='source-token',bot=False):
 return {'sender':{'user_id':123,'is_bot':bot},'recipient':{'chat_id':chat},
         'body':{'mid':mid,'attachments':[{'type':'file','filename':'Заявка.pdf','size':1000,
                                          'payload':{'url':'https://fu.oneme.ru/download','token':token}}]}}

class MaxTests(unittest.TestCase):
 def setUp(self):
  self.base=ROOT/'tmp'/('max-test-'+uuid.uuid4().hex); self.base.mkdir()
  self.store=Store(self.base/'state.sqlite3')
  self.api=FakeAPI(ROOT/'private/unsigned.pdf')
  self.process_calls=0
  self.workflow=Workflow(self.store,self.api,CHATS,APPROVER,ROOT/'config.local.json',self.base/'jobs',self.fake_processor)
 def fake_processor(self,source,config,output):
  self.process_calls+=1; output.mkdir()
  target=output/'request_SIGNED.pdf'; target.write_bytes(source.read_bytes())
  return target,[{'page':i+1,'status':'SIGNED','reason':'Confirmed'} for i in range(4)]
 def add(self,msg=None):
  msg=msg or message(); self.api.messages[msg['body']['mid']]=msg
  self.workflow.handle({'update_type':'message_created','message':msg})
  with self.store.connect() as db:
   return db.execute('SELECT * FROM jobs WHERE mid=?',(msg['body']['mid'],)).fetchone()
 def callback(self,job,user=APPROVER,mid=None,action='sign',profile=None):
  return {'update_type':'message_callback','callback':{'callback_id':uuid.uuid4().hex,
          'user':{'user_id':user,**(profile or {})},'payload':action+':'+job['id']},
          'message':{'body':{'mid':mid or job['notification_mid']}}}
 def test_private_button_only_to_vitaliy(self):
  job=self.add()
  self.assertEqual(job['state'],'WAITING'); self.assertEqual(self.process_calls,0)
  self.assertEqual(self.api.sent[0]['user_id'],APPROVER)
  self.assertNotIn('chat_id',self.api.sent[0])
 def test_two_approvers_first_confirmation_wins(self):
  extra=149086145
  self.workflow.approvers={c:{APPROVER,extra} for c in CHATS}
  job=self.add()
  self.assertEqual({s['user_id'] for s in self.api.sent},{APPROVER,extra})
  with self.store.connect() as db:
   notice=db.execute('SELECT mid FROM notifications WHERE job=? AND user=?',(job['id'],extra)).fetchone()
  self.workflow.handle(self.callback(job,user=extra,mid=notice['mid']))
  self.workflow.handle(self.callback(job))
  self.assertEqual(self.store.job(job['id'])['approver'],extra)
  self.assertEqual(self.store.job(job['id'])['state'],'SENT')
  self.assertEqual(self.process_calls,1)
  self.assertEqual(len([s for s in self.api.sent if 'chat_id' in s]),1)
  self.assertIn('Уже подписано',self.api.answers[-1][1])
 def test_approver_is_scoped_to_chat(self):
  extra=149086145
  self.workflow.approvers={CHATS[0]:{APPROVER,extra},CHATS[1]:{APPROVER}}
  job=self.add(message(chat=CHATS[1]))
  self.workflow.handle(self.callback(job,user=extra))
  self.assertEqual(self.store.job(job['id'])['state'],'WAITING')
  self.assertEqual(self.process_calls,0)
 def test_other_approver_cannot_use_forwarded_button(self):
  extra=149086145
  self.workflow.approvers={c:{APPROVER,extra} for c in CHATS}
  job=self.add()
  self.workflow.handle(self.callback(job,user=extra,mid=job['notification_mid']))
  self.assertEqual(self.store.job(job['id'])['state'],'WAITING')
 def test_new_user_gets_id_without_access(self):
  self.workflow.handle({'update_type':'bot_started','user':{'user_id':987}})
  self.assertEqual(self.api.sent[0]['user_id'],987)
  self.assertIn('987',self.api.sent[0]['text'])
  self.assertNotIn('attachments',self.api.sent[0] if self.api.sent[0].get('attachments') else {})
 def test_private_id_command(self):
  self.workflow.handle({'update_type':'message_created','message':{'sender':{'user_id':987},'recipient':{'chat_type':'dialog'},'body':{'text':'/id'}}})
  self.assertEqual(self.api.sent[0]['text'],'Ваш MAX ID: 987')
 def test_unauthorized_press_does_not_sign(self):
  job=self.add(); self.workflow.handle(self.callback(job,user=55))
  self.assertEqual(self.process_calls,0); self.assertEqual(self.store.job(job['id'])['state'],'WAITING')
 def test_forwarded_button_does_not_sign(self):
  job=self.add(); self.workflow.handle(self.callback(job,mid='forwarded'))
  self.assertEqual(self.process_calls,0)
 def test_two_chats_reply_only_to_origin(self):
  for i,chat in enumerate(CHATS):
   job=self.add(message(mid='source-'+str(i),chat=chat))
   self.workflow.handle(self.callback(job))
   groups=[m for m in self.api.sent if 'chat_id' in m]
   self.assertEqual(groups[-1]['chat_id'],chat)
   self.assertEqual(groups[-1]['reply_to'],job['mid'])
   self.assertEqual(self.store.job(job['id'])['state'],'SENT')
 def test_duplicate_event_and_press(self):
  msg=message(); job=self.add(msg); self.add(msg)
  self.workflow.handle(self.callback(job)); self.workflow.handle(self.callback(job))
  self.assertEqual(self.process_calls,1)
  self.assertEqual(len([m for m in self.api.sent if 'chat_id' in m]),1)
 def test_duplicate_bytes_new_message_not_republished(self):
  first=self.add(); self.workflow.handle(self.callback(first))
  second=self.add(message(mid='again')); self.workflow.handle(self.callback(second))
  self.assertEqual(self.store.job(second['id'])['state'],'DUPLICATE')
  self.assertEqual(len(self.api.uploaded),1)
 def test_changed_attachment_cancels(self):
  job=self.add(); self.api.messages[job['mid']]=message(token='changed')
  self.workflow.handle(self.callback(job))
  self.assertEqual(self.process_calls,0); self.assertEqual(self.store.job(job['id'])['state'],'CANCELLED')
 def test_refreshed_max_token_keeps_same_file(self):
  msg=message(); msg['body']['attachments'][0]['payload']['fileId']='stable-file'
  job=self.add(msg)
  self.api.messages[job['mid']]['body']['attachments'][0]['payload']['token']='refreshed-token'
  self.workflow.handle(self.callback(job))
  self.assertEqual(self.store.job(job['id'])['state'],'SENT')
 def test_same_file_id_changed_bytes_cancels(self):
  msg=message(); msg['body']['attachments'][0]['payload']['fileId']='stable-file'
  job=self.add(msg); self.api.source=ROOT/'private/negative.pdf'
  self.workflow.handle(self.callback(job))
  self.assertEqual(self.process_calls,0)
  self.assertEqual(self.store.job(job['id'])['state'],'CANCELLED')
 def test_edited_event_cancels_old_button(self):
  job=self.add(); changed=message(token='changed')
  self.workflow.handle({'update_type':'message_edited','message':changed})
  self.workflow.handle(self.callback(job))
  self.assertEqual(self.process_calls,0); self.assertEqual(self.store.job(job['id'])['state'],'CANCELLED')
 def test_unknown_delivery_not_retried_even_after_restart(self):
  job=self.add(); self.api.fail_group=True; self.workflow.handle(self.callback(job))
  self.store.recover(); self.workflow.handle(self.callback(job))
  self.assertEqual(self.store.job(job['id'])['state'],'DELIVERY_UNKNOWN')
  self.assertEqual(len([m for m in self.api.sent if 'chat_id' in m]),1)
 def test_source_pdf_is_deleted_after_completion(self):
  job=self.add(); source=self.workflow.workdir/job['id']/'request.pdf'
  self.assertTrue(source.exists()); self.workflow.handle(self.callback(job)); self.assertFalse(source.exists())
 def test_rejected_page_not_posted_to_group(self):
  def reject(source,config,output):
   return source,[{'page':1,'status':'REVIEW_REQUIRED','reason':'Wrong INN'}]
  self.workflow.processor=reject
  job=self.add(); self.workflow.handle(self.callback(job))
  self.assertEqual(self.store.job(job['id'])['state'],'REVIEW_REQUIRED')
  self.assertFalse(self.api.uploaded)
  self.assertFalse(any('chat_id' in m for m in self.api.sent))
 def test_press_records_who_and_when(self):
  job=self.add(); before=time.time()
  self.workflow.handle(self.callback(job,profile={'first_name':'Иван','last_name':'Петров'}))
  saved=self.store.job(job['id'])
  self.assertEqual((saved['state'],saved['approver'],saved['approver_name']),('SENT',APPROVER,'Иван Петров'))
  self.assertGreaterEqual(saved['approved_at'],before)
  self.assertIn('Файл: Заявка.pdf\nНажал: Иван Петров (ID 33162943)',self.api.sent[-1]['text'])
  self.workflow.handle(self.callback(job))
  self.assertEqual(self.api.answers[-1][1],'Уже подписано: Иван Петров (ID 33162943)')
 def test_rejection_names_who_pressed_without_profile(self):
  self.workflow.processor=lambda source,config,output:(source,[{'page':1,'status':'REVIEW_REQUIRED','reason':'Wrong INN'}])
  job=self.add(); self.workflow.handle(self.callback(job))
  self.assertTrue(self.api.sent[-1]['text'].endswith('Нажал: ID 33162943'))
  self.assertIsNone(self.workflow.pressed)
 def test_old_database_gets_new_columns(self):
  path=self.base/'old.sqlite3'
  import sqlite3
  with sqlite3.connect(path) as db:
   db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,chat INTEGER,mid TEXT,attachment TEXT,filename TEXT,state TEXT,notification_mid TEXT,source_sha TEXT,result_mid TEXT,reason TEXT,created REAL,approver INTEGER,UNIQUE(chat,mid,attachment))')
  Store(path)
  with sqlite3.connect(path) as db:
   columns={row[1] for row in db.execute('PRAGMA table_info(jobs)')}
  self.assertTrue({'approved_at','approver_name'}<=columns)
 def test_real_negative_not_published(self):
  self.api.source=ROOT/'private/negative.pdf'; self.workflow.processor=run_signer
  job=self.add(); self.workflow.handle(self.callback(job))
  self.assertEqual(self.store.job(job['id'])['state'],'REVIEW_REQUIRED')
  self.assertFalse(self.api.uploaded)
 def test_real_pdf_workflow(self):
  self.workflow.processor=run_signer
  job=self.add(); self.workflow.handle(self.callback(job))
  self.assertEqual(self.store.job(job['id'])['state'],'SENT')
  from pypdf import PdfReader
  import io
  pdf=PdfReader(io.BytesIO(self.api.uploaded[0][0]))
  self.assertEqual(len(pdf.pages),4)
  self.assertTrue(all(p.get('/Annots') for p in pdf.pages))
  self.assertEqual(self.api.uploaded[0][1],'Заявка_SIGNED.pdf')
 def test_other_chats_and_bot_files_ignored(self):
  self.workflow.handle({'update_type':'message_created','message':message(chat=-999)})
  self.workflow.handle({'update_type':'message_created','message':message(bot=True)})
  self.assertFalse(self.api.sent)
 def test_skip_and_expiry(self):
  job=self.add(); self.workflow.handle(self.callback(job,action='skip'))
  self.assertEqual(self.store.job(job['id'])['state'],'SKIPPED'); self.assertEqual(self.process_calls,0)
  second=self.add(message(mid='expired'))
  with self.store.connect() as db: db.execute('UPDATE jobs SET created=0 WHERE id=?',(second['id'],))
  self.workflow.handle(self.callback(second)); self.assertEqual(self.store.job(second['id'])['state'],'EXPIRED')
 def test_inbox_deduplicates_and_persists(self):
  event={'update_type':'message_created','message':message()}
  self.api.messages['incoming']=event['message']
  self.store.enqueue(event); self.store.enqueue(event)
  self.assertTrue(self.workflow.tick()); self.assertFalse(self.workflow.tick())
  with self.store.connect() as db:
   rows=db.execute('SELECT * FROM events').fetchall()
  self.assertEqual(len(rows),1); self.assertEqual(rows[0]['state'],'DONE'); self.assertIsNone(rows[0]['payload'])
 def test_webhook_secret(self):
  self.assertFalse(authorized('','')); self.assertFalse(authorized('wrong','right')); self.assertTrue(authorized('correct','correct'))
 def test_media_url_rejects_external_or_private(self):
  for url in ['http://fu.oneme.ru/a','https://evil.example/a','https://oneme.ru.evil.example/a','https://token@fu.oneme.ru/a','https://127.0.0.1/a']:
   with self.assertRaises(ValueError): validate_media_url(url)
  with patch('max_api.socket.getaddrinfo',return_value=[(None,None,None,None,('127.0.0.1',443))]):
   with self.assertRaises(ValueError): validate_media_url('https://fu.oneme.ru/a')
 def test_notification_timeout_does_not_duplicate_button(self):
  self.api.fail_private=True; msg=message()
  with self.assertRaises(TimeoutError): self.add(msg)
  self.workflow.incoming(msg)
  self.assertEqual(len(self.api.sent),1)

if __name__=='__main__': unittest.main(verbosity=2)
