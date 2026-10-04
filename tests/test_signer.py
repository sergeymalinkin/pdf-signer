import hashlib
import json
import tempfile
import unittest
import uuid
import re
from pathlib import Path

import pdfplumber
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from signer import process, inspect_page, appearance_digest

def real_negative(source, target):
 from pypdf import PdfWriter
 from pypdf.generic import ContentStream, TextStringObject, ByteStringObject, NameObject
 writer=PdfWriter(clone_from=source)
 for page in writer.pages:
  maps={}
  for name,ref in page['/Resources']['/Font'].items():
   cmap=ref.get_object()['/ToUnicode'].get_data().decode()
   maps[name]={bytes.fromhex(b).decode('utf-16-be'):bytes.fromhex(a) for a,b in re.findall(r'<([0-9a-fA-F]{2})>\s*<([0-9a-fA-F]{4})>',cmap)}
  content=ContentStream(page.get_contents(),writer)
  font=None; changes=0
  for operands,operator in content.operations:
   if operator==b'Tf': font=operands[0]
   if operator!=b'TJ' or font not in maps: continue
   arr=operands[0]
   raw=[v.original_bytes if isinstance(v,TextStringObject) else bytes(v) if isinstance(v,ByteStringObject) else b'' for v in arr]
   try: needle=b''.join(maps[font][v] for v in '4345494742')
   except KeyError: continue
   joined=b''.join(raw); start=joined.find(needle)
   if start<0: continue
   position=start+len(needle)-1; offset=0
   for index,value in enumerate(raw):
    if offset <= position < offset+len(value):
     local=position-offset
     arr[index]=ByteStringObject(value[:local]+maps[font]['3']+value[local+1:])
     changes+=1; break
    offset+=len(value)
  if changes!=1: raise AssertionError('Expected one real carrier INN replacement per page')
  page[NameObject('/Contents')]=writer._add_object(content)
 writer.write(target)

ROOT=Path(__file__).resolve().parents[1]
pdfmetrics.registerFont(TTFont('TestArial',r'C:\Windows\Fonts\arial.ttf'))

class SignerTests(unittest.TestCase):
 def setUp(self):
  self.base=ROOT/'tmp'/('test-'+uuid.uuid4().hex)
  self.base.mkdir()
  self.cfg=json.loads((ROOT/'config.local.json').read_text(encoding='utf-8'))
  self.cfg['facsimile']=str(ROOT/'private/facsimile.png')
  self.config=self.base/'config.json'
  self.config.write_text(json.dumps(self.cfg,ensure_ascii=False),encoding='utf-8')
 def tearDown(self): pass  # Keep ignored test artifacts available for inspection.
 def fixture(self,inn='4345494742',name='ООО «Железнодорожная логистика»',structure=True,pages=None,header=500):
  path=self.base/'input.pdf'
  c=Canvas(str(path),pagesize=(595.32,841.92))
  for val in pages or [inn]:
   c.setFont('TestArial',8)
   def text(x,top,value): c.drawString(x,841.92-top,value)
   text(100,35,'ЗАЯВКА № TEST-001 3 октября 2026 г.')
   text(28.56,300,'Перевозчик:')
   text(180,310,name+((', ИНН: '+val) if val else '')+', адрес: Киров')
   text(180,400,'Купченко Валерий Григорьевич в/у 123')
   text(28,420,'Марка/гос. номер Рено Х799ХН152 Тип владения автомобиля аренда')
   if structure:
    text(256.44,header,'ПОДПИСИ СТОРОН')
    text(370.92,header+19.8,'Перевозчик')
   c.showPage()
  c.save()
  return path
 def run_pdf(self,path,inspector=inspect_page,folder='out'):
  return process(path,self.config,self.base/folder,inspector)
 def assert_no_stamp(self,path,page=0):
  self.assertFalse(PdfReader(path).pages[page].get('/Annots'))
 def test_01_correct_identity_signed_and_visible_stream(self):
  out,rows=self.run_pdf(self.fixture())
  self.assertEqual(rows[0]['status'],'SIGNED')
  a=PdfReader(out).pages[0]['/Annots'][0].get_object()
  self.assertTrue(a['/AP']['/N'].get_data())
  self.assertTrue(a['/AP']['/N']['/Resources']['/XObject']['/Facsimile'].get_data())
 def test_02_wrong_inn_no_signature(self):
  out,rows=self.run_pdf(self.fixture(inn='4345494743'))
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED')
  self.assertEqual(rows[0]['inn_found'],'4345494743')
  self.assert_no_stamp(out)
 def test_03_missing_inn(self):
  out,rows=self.run_pdf(self.fixture(inn=None))
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED'); self.assert_no_stamp(out)
 def test_04_wrong_name_with_correct_inn(self):
  out,rows=self.run_pdf(self.fixture(name='ООО «Другая компания»'))
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED'); self.assert_no_stamp(out)
 def test_05_normalization(self):
  for i,name in enumerate(['ооо   “ЖЕЛЕЗНОДОРОЖНАЯ   логистика”','Общество с ограниченной ответственностью «Железнодорожная логистика»']):
   out,rows=self.run_pdf(self.fixture(name=name),folder=str(i))
   self.assertEqual(rows[0]['status'],'SIGNED')
 def test_06_missing_structure(self):
  out,rows=self.run_pdf(self.fixture(structure=False))
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED'); self.assert_no_stamp(out)
 def test_07_page_exception_is_logged_and_next_page_processed(self):
  def fail_first(page,p,cfg):
   if p.page_number==1: raise RuntimeError('Injected failure')
   return inspect_page(page,p,cfg)
  out,rows=self.run_pdf(self.fixture(pages=['4345494742']*2),fail_first)
  self.assertEqual([r['status'] for r in rows],['ERROR','SIGNED']); self.assert_no_stamp(out)
  self.assertIn('RuntimeError',rows[0]['reason'])
 def test_08_source_unchanged(self):
  path=ROOT/'private/unsigned.pdf'; original=path.read_bytes()
  out,rows=self.run_pdf(path)
  self.assertEqual(path.read_bytes(),original)
  self.assertEqual(rows[0]['driver'],'Купченко Валерий Григорьевич')
  self.assertEqual(rows[0]['vehicle'],'Рено Х799ХН152')
  a,b=PdfReader(path),PdfReader(out)
  self.assertEqual(len(b.pages),4)
  with pdfplumber.open(path) as x,pdfplumber.open(out) as y:
   self.assertEqual([p.extract_text() for p in x.pages],[p.extract_text() for p in y.pages])
  for left,right in zip(a.pages,b.pages):
   self.assertEqual(left.get_contents().get_data(),right.get_contents().get_data())
   self.assertEqual(list(left.mediabox),list(right.mediabox))
 def test_09_reference_and_repeat_do_not_double_overlay(self):
  for i,path in enumerate([ROOT/'private/reference.pdf',ROOT/'private/unsigned.pdf']):
   out,rows=self.run_pdf(path,folder='first'+str(i))
   repeated,again=self.run_pdf(out,folder='second'+str(i))
   self.assertEqual([r['status'] for r in again],['ALREADY_SIGNED']*4)
   a,b=PdfReader(out),PdfReader(repeated)
   for left,right in zip(a.pages,b.pages):
    la=[v.get_object() for v in left.get('/Annots',[]) if v.get_object().get('/Subtype')=='/Stamp']
    ra=[v.get_object() for v in right.get('/Annots',[]) if v.get_object().get('/Subtype')=='/Stamp']
    self.assertEqual(len(la),1); self.assertEqual(len(ra),1)
    self.assertEqual(appearance_digest(la[0]['/AP']['/N']),appearance_digest(ra[0]['/AP']['/N']))
 def test_10_mixed_pages_independent(self):
  out,rows=self.run_pdf(self.fixture(pages=['4345494742','4345494743',None,'4345494742']))
  self.assertEqual([r['status'] for r in rows],['SIGNED','REVIEW_REQUIRED','REVIEW_REQUIRED','SIGNED'])
  self.assert_no_stamp(out,1); self.assert_no_stamp(out,2)
 def test_11_duplicate_inn_is_ambiguous(self):
  out,rows=self.run_pdf(self.fixture(inn='4345494742, ИНН: 4345494743'))
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED'); self.assert_no_stamp(out)
 def test_12_existing_output_is_not_overwritten(self):
  path=self.fixture(); self.run_pdf(path)
  with self.assertRaises(FileExistsError): self.run_pdf(path)
 def test_13_unknown_stamp_is_review(self):
  from pypdf import PdfWriter
  from pypdf.generic import NameObject
  out,_=self.run_pdf(self.fixture())
  writer=PdfWriter(clone_from=out)
  a=writer.pages[0]['/Annots'][0].get_object()
  a['/AP']['/N'].set_data(b'q Q')
  changed=self.base/'unknown.pdf'
  writer.write(changed)
  _,rows=self.run_pdf(changed,folder='unknown')
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED')
 def test_14_real_four_page_negative(self):
  negative=self.base/'negative.pdf'
  real_negative(ROOT/'private/unsigned.pdf',negative)
  out,rows=self.run_pdf(negative)
  self.assertEqual([r['inn_found'] for r in rows],['4345494743']*4)
  self.assertEqual([r['status'] for r in rows],['REVIEW_REQUIRED']*4)
  for index in range(4): self.assert_no_stamp(out,index)
 def test_15_hidden_or_resized_stamp_is_review(self):
  from pypdf import PdfWriter
  from pypdf.generic import NameObject, NumberObject, ArrayObject, FloatObject
  out,_=self.run_pdf(self.fixture())
  for i,kind in enumerate(['hidden','resized']):
   writer=PdfWriter(clone_from=out)
   stamp=writer.pages[0]['/Annots'][0].get_object()
   if kind=='hidden': stamp[NameObject('/F')]=NumberObject(6)
   else: stamp[NameObject('/Rect')]=ArrayObject([FloatObject(v) for v in [370,250,371,251]])
   path=self.base/(kind+'.pdf'); writer.write(path)
   _,rows=self.run_pdf(path,folder=kind)
   self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED')
 def test_17_real_mixed_pages_preserve_existing_signature(self):
  from pypdf import PdfWriter
  signed=PdfReader(ROOT/'private/marks-check.pdf')
  unsigned=PdfReader(ROOT/'private/layout-second.pdf')
  writer=PdfWriter(); writer.add_page(signed.pages[0])
  writer.add_page(unsigned.pages[1]); writer.add_page(unsigned.pages[2])
  source=self.base/'mixed.pdf'; writer.write(source)
  original=PdfReader(source)
  original_appearance=appearance_digest(original.pages[0]['/Annots'][0].get_object()['/AP']['/N'])
  out,rows=self.run_pdf(source)
  self.assertEqual([r['status'] for r in rows],['ALREADY_SIGNED','SIGNED','SIGNED'])
  result=PdfReader(out)
  self.assertEqual(result.pages[0].get_contents().get_data(),original.pages[0].get_contents().get_data())
  self.assertEqual(appearance_digest(result.pages[0]['/Annots'][0].get_object()['/AP']['/N']),original_appearance)
  self.assertEqual(len(result.pages[0]['/Annots']),len(original.pages[0]['/Annots']))
  repeat,repeated=self.run_pdf(out,folder='mixed-repeat')
  self.assertEqual([r['status'] for r in repeated],['ALREADY_SIGNED']*3)
  self.assertEqual([len(p['/Annots']) for p in PdfReader(repeat).pages],
                   [len(p['/Annots']) for p in result.pages])
 def test_18_long_table_and_rounded_a4(self):
  from pypdf import PdfWriter
  from pypdf.generic import RectangleObject
  source=self.fixture(header=620)
  writer=PdfWriter(clone_from=source)
  writer.pages[0].mediabox=RectangleObject([0,0,595,841])
  writer.pages[0].cropbox=RectangleObject([0,0,595,841])
  rounded=self.base/'rounded.pdf'; writer.write(rounded)
  _,rows=self.run_pdf(rounded)
  self.assertEqual(rows[0]['status'],'SIGNED')
 def test_19_signature_zone_must_fit_page(self):
  _,rows=self.run_pdf(self.fixture(header=760))
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED')
 def test_20_embedded_real_signature_preserved(self):
  out,rows=self.run_pdf(ROOT/'private/today-check.pdf')
  self.assertEqual([r['status'] for r in rows],['ALREADY_SIGNED']*2)
  for i in range(2): self.assert_no_stamp(out,i)
 def test_21_unknown_embedded_signature_stays_review(self):
  self.cfg['embedded_signatures'][0]['digest']='0'*64
  self.config.write_text(json.dumps(self.cfg,ensure_ascii=False),encoding='utf-8')
  _,rows=self.run_pdf(ROOT/'private/today-check.pdf')
  self.assertEqual([r['status'] for r in rows],['REVIEW_REQUIRED']*2)
 def test_22_embedded_mixed_pdf_and_repeat(self):
  from pypdf import PdfWriter
  existing=PdfReader(ROOT/'private/today-check.pdf')
  blank=PdfReader(ROOT/'private/layout-second.pdf')
  w=PdfWriter(); w.add_page(existing.pages[0]); w.add_page(blank.pages[1])
  source=self.base/'embedded-mixed.pdf'; w.write(source)
  out,rows=self.run_pdf(source)
  self.assertEqual([r['status'] for r in rows],['ALREADY_SIGNED','SIGNED'])
  self.assertEqual(PdfReader(source).pages[0].get_contents().get_data(),PdfReader(out).pages[0].get_contents().get_data())
  _,again=self.run_pdf(out,folder='again')
  self.assertEqual([r['status'] for r in again],['ALREADY_SIGNED']*2)
 def test_23_extra_text_near_embedded_signature_rejected(self):
  self.cfg['embedded_signatures'][0]['text_hashes']=[]
  self.config.write_text(json.dumps(self.cfg,ensure_ascii=False),encoding='utf-8')
  _,rows=self.run_pdf(ROOT/'private/today-check.pdf')
  self.assertEqual([r['status'] for r in rows],['REVIEW_REQUIRED']*2)
 def test_16_rotation_is_preserved_but_not_blindly_signed(self):
  from pypdf import PdfWriter
  source=self.fixture(); writer=PdfWriter(clone_from=source); writer.pages[0].rotate(90)
  path=self.base/'rotated.pdf'; writer.write(path)
  out,rows=self.run_pdf(path)
  self.assertEqual(rows[0]['status'],'REVIEW_REQUIRED'); self.assert_no_stamp(out)
  self.assertEqual(PdfReader(out).pages[0].get('/Rotate'),90)

if __name__=='__main__': unittest.main(verbosity=2)
