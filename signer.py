"""Fail-closed local visual PDF signer. No OCR or network access."""
import argparse
import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import pdfplumber
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          FloatObject, NameObject, NumberObject)


def normalize(value):
    value = unicodedata.normalize('NFKC', value).casefold()
    value = re.sub(r'общество\s+с\s+ограниченной\s+ответственностью', 'ооо', value)
    value = re.sub('[«»“”„"\u2018\u2019]', '', value)
    return re.sub(r'\s+', ' ', value).strip()


def appearance_digest(form):
    """Exact executable appearance and image data; no personal metadata."""
    form = form.get_object()
    parts = [form.get_data(), str(form.get('/BBox')).encode(),
             str(form.get('/Matrix')).encode()]
    resources = form.get('/Resources', {}).get_object() if '/Resources' in form else {}
    for name, ref in sorted(resources.get('/XObject', {}).items()):
        obj = ref.get_object()
        parts.extend([str(name).encode(), str(obj.get('/Subtype')).encode()])
        if obj.get('/Subtype') == '/Form':
            parts.append(appearance_digest(obj).encode())
        else:
            parts.extend([obj.get_data(), str(obj.get('/Width')).encode(),
                          str(obj.get('/Height')).encode(), str(obj.get('/ColorSpace')).encode(),
                          str(obj.get('/BitsPerComponent')).encode()])
            if '/SMask' in obj:
                parts.append(obj['/SMask'].get_data())
            parts.append(str(obj.get('/Mask')).encode())
    return hashlib.sha256(b'\0'.join(parts)).hexdigest()


def number_array(values):
    return ArrayObject([FloatObject(v) for v in values])


def make_stamp(image_path, rect):
    with Image.open(image_path) as source:
        if source.mode not in ('RGB', 'RGBA'):
            raise ValueError('Facsimile must be RGB or RGBA PNG')
        im = source.convert('RGB')
        xobj = DecodedStreamObject()
        xobj.set_data(im.tobytes())
        xobj.update({NameObject('/Type'): NameObject('/XObject'),
                     NameObject('/Subtype'): NameObject('/Image'),
                     NameObject('/Width'): NumberObject(im.width),
                     NameObject('/Height'): NumberObject(im.height),
                     NameObject('/ColorSpace'): NameObject('/DeviceRGB'),
                     NameObject('/BitsPerComponent'): NumberObject(8)})
        # PDF transparency, without changing the customer image file.
        if source.mode == 'RGBA':
            alpha = DecodedStreamObject()
            alpha.set_data(source.getchannel('A').tobytes())
            alpha.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                          NameObject('/Width'): NumberObject(im.width), NameObject('/Height'): NumberObject(im.height),
                          NameObject('/ColorSpace'): NameObject('/DeviceGray'), NameObject('/BitsPerComponent'): NumberObject(8)})
            xobj[NameObject('/SMask')] = alpha
        else:
            xobj[NameObject('/Mask')] = ArrayObject([NumberObject(v) for v in [255,255]*3])
        width, height = rect[2]-rect[0], rect[3]-rect[1]
        form = DecodedStreamObject()
        form.set_data(f'q {width:.6f} 0 0 {height:.6f} 0 0 cm /Facsimile Do Q'.encode())
        form.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Form'),
                     NameObject('/BBox'): number_array([0,0,width,height]),
                     NameObject('/Resources'): DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/Facsimile'): xobj})})})
        return DictionaryObject({NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Stamp'),
                                 NameObject('/Rect'): number_array(rect), NameObject('/F'): NumberObject(4),
                                 NameObject('/AP'): DictionaryObject({NameObject('/N'): form})})


def overlaps(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def embedded_digest(obj):
    stream=obj['stream']
    attrs=stream.attrs
    parts=[stream.get_data()]
    for key in ('Width','Height','BitsPerComponent','ColorSpace','ImageMask','Decode','Mask'):
        parts.append(repr(attrs.get(key)).encode())
    from pdfminer.pdftypes import resolve1
    mask=resolve1(attrs.get('SMask'))
    if mask is not None:
        parts.append(mask.get_data())
        for key in ('Width','Height','BitsPerComponent','ColorSpace','ImageMask','Decode','Mask'):
            parts.append(repr(mask.attrs.get(key)).encode())
    return hashlib.sha256(b'\0'.join(parts)).hexdigest()


def residual_signature_text(text):
    return re.sub(r'[\s_/]', '', text.replace('Перевозчик', '').replace('м.п.', ''))


def inspect_page(page, pdf_page, cfg):
    result = dict(request_number=None, date=None, carrier_name_raw=None, carrier_name_normalized=None,
                  inn_found=None, inn_expected=cfg['inn'], driver=None, vehicle=None,
                  extraction_method='pdf_text_layer', status='REVIEW_REQUIRED', reason='Unconfirmed page')
    def reject(reason):
        result['reason'] = reason
        return result, None
    text = pdf_page.extract_text() or ''
    if not text.strip():
        result['extraction_method'] = 'none'
        return reject('No readable text layer; OCR is not enabled')
    first = text.splitlines()[0]
    match = re.search(r'№\s*(\S+)\s+(.+)', first)
    if match:
        result['request_number'], result['date'] = match.groups()
    driver = re.search(r'([А-ЯЁа-яё]+[^\S\r\n]+[А-ЯЁа-яё]+[^\S\r\n]+[А-ЯЁа-яё]+)[^\S\r\n]+в/у', text)
    vehicle = re.search(r'Марка/гос\.\s*номер\s+(.+?)\s+Тип владения', text)
    result['driver'] = driver.group(1) if driver else None
    result['vehicle'] = vehicle.group(1) if vehicle else None
    words = pdf_page.extract_words()
    carrier = [w for w in words if w['text'] == 'Перевозчик:']
    headers = [w for w in words if w['text'] == 'ПОДПИСИ']
    labels = [w for w in words if w['text'] == 'Перевозчик']
    sides = [w for w in words if w['text'] == 'СТОРОН']
    if len(carrier) != 1:
        return reject('Carrier identity block is absent or ambiguous')
    y = carrier[0]['top']
    raw = pdf_page.crop((cfg['identity_x'][0], y+cfg['identity_y'][0], cfg['identity_x'][1], y+cfg['identity_y'][1])).extract_text() or ''
    if raw:
        result['carrier_name_raw'] = raw.splitlines()[0].split(',')[0].strip()
        result['carrier_name_normalized'] = normalize(result['carrier_name_raw'])
    names = re.findall(r'(.+?),\s*ИНН\s*:\s*(\d+)(?!\d)', raw)
    if len(names) != 1 or len(re.findall(r'ИНН\s*:', raw)) != 1:
        return reject('Carrier name and INN pair is absent or ambiguous')
    result['carrier_name_raw'], result['inn_found'] = names[0]
    result['carrier_name_normalized'] = normalize(names[0][0])
    if result['inn_found'] != cfg['inn']:
        return reject('Carrier INN does not match configured INN')
    if result['carrier_name_normalized'] != normalize(cfg['company']):
        return reject('Carrier legal name does not match configured name')
    if len(headers) != 1 or len(labels) != 1 or len(sides) != 1:
        return reject('Signature block is absent or ambiguous')
    h, label, side = headers[0], labels[0], sides[0]
    if not (abs(side['top']-h['top']) < 1 and 0 < side['x0']-h['x0'] < 60
            and abs(label['top']-h['top']-cfg['label_offset']) < 1
            and abs(label['x0']-cfg['label_x']) < 1
            and abs(h['x0']-cfg.get('header_x',256.44)) < 1
            and cfg['header_range'][0] <= h['top']
            and h['top']+cfg['zone_y'][1] <= pdf_page.height
            and h['top'] > y+100):
        return reject('Signature anchors do not match verified layout')
    geometry = [float(v) for v in page.mediabox]
    # Exporters may round A4 dimensions to whole points; coordinates remain checked.
    if (any(abs(a-b) > 1 for a,b in zip(geometry,cfg['mediabox']))
            or int(page.get('/Rotate',0)) != cfg['rotation']
            or list(page.cropbox) != list(page.mediabox)
            or float(page.get('/UserUnit',1)) != 1):
        return reject('Page geometry is outside verified profile')
    # Informational data, bounded to known table rows.
    zone = [cfg['zone_x'][0], h['top']+cfg['zone_y'][0], cfg['zone_x'][1], h['top']+cfg['zone_y'][1]]
    rect = [cfg['stamp_x'], pdf_page.height-(h['top']+cfg['stamp_y']+cfg['stamp_height']),
            cfg['stamp_x']+cfg['stamp_width'], pdf_page.height-(h['top']+cfg['stamp_y'])]
    expected = appearance_digest(make_stamp(cfg['facsimile'], rect)['/AP']['/N'])
    reference_appearances = set(cfg.get('reference_appearances', []))
    recognized = 0
    uncertain = False
    zone_text = pdf_page.crop(tuple(zone)).extract_text() or ''
    embedded=[]
    for obj in pdf_page.images:
        if not overlaps([obj['x0'],obj['top'],obj['x1'],obj['bottom']],zone): continue
        values=[obj['x0'],obj['top']-h['top'],obj['x1']-obj['x0'],obj['bottom']-obj['top']]
        for profile in cfg.get('embedded_signatures',[]):
            if (embedded_digest(obj)==profile['digest']
                    and all(low<=value<=high for value,(low,high) in zip(values,profile['bounds']))
                    and obj['x0']>=zone[0]-1 and obj['x1']<=zone[2]
                    and obj['top']>=zone[1] and obj['bottom']<=zone[3]):
                embedded.append((obj,profile)); break
    residual=residual_signature_text(zone_text)
    text_hash=hashlib.sha256(residual.encode()).hexdigest()
    if residual and not (len(embedded)==1 and text_hash in embedded[0][1]['text_hashes']):
        uncertain = True
    recognized+=len(embedded)
    for ref in page.get('/Annots', []):
        annot = ref.get_object()
        ar = [float(v) for v in annot.get('/Rect', [0,0,0,0])]
        top_rect = [ar[0],pdf_page.height-ar[3],ar[2],pdf_page.height-ar[1]]
        if not overlaps(top_rect, zone):
            if annot.get('/Subtype') == '/Stamp':
                uncertain = True
            continue
        normal = annot.get('/AP', {}).get('/N')
        visible = (int(annot.get('/F', 0)) & 4 and not int(annot.get('/F', 0)) & 35
                   and float(annot.get('/CA', 1)) == 1 and '/OC' not in annot)
        own_placement = all(abs(a-b) < .01 for a,b in zip(ar, rect))
        ref_bounds = cfg['reference_placement_bounds']
        ref_values = [top_rect[0], top_rect[1]-h['top'], ar[2]-ar[0], ar[3]-ar[1]]
        ref_placement = all(low <= value <= high for value,(low,high) in zip(ref_values,ref_bounds))
        digest = appearance_digest(normal) if normal is not None else None
        approved = (digest == expected and own_placement) or (digest in reference_appearances and ref_placement)
        if (annot.get('/Subtype') == '/Stamp' and normal is not None
                and visible and approved
                and zone[0] <= top_rect[0] and top_rect[2] <= zone[2]
                and zone[1] <= top_rect[1] and top_rect[3] <= zone[3]):
            recognized += 1
        else:
            uncertain = True
    # Flattened images/vector marks are not treated as reliably signed.
    for obj in pdf_page.images + pdf_page.curves:
        if overlaps([obj['x0'],obj['top'],obj['x1'],obj['bottom']], zone):
            if any(obj is approved for approved,_ in embedded): continue
            uncertain = True
    for obj in pdf_page.lines + pdf_page.rects:
        # The supplied signed reference has a white page background rectangle.
        background = cfg['reference_background']
        embedded_background=any(all(abs(a-b)<.2 for a,b in zip(
            [obj['x0'],obj['top'],obj['x1'],obj['bottom']-h['top']],profile['background']))
            for _,profile in embedded)
        if (obj['object_type'] == 'rect' and obj.get('stroking_color') == (1.,1.,1.)
                and obj.get('non_stroking_color') == (1.,1.,1.)
                and (embedded_background or all(abs(a-b) < .2 for a,b in zip(
                    [obj['x0'],obj['top'],obj['x1'],obj['bottom']-h['top']], background)))):
            continue
        if obj['bottom'] > h['top']+1 and overlaps([obj['x0'],obj['top'],obj['x1'],obj['bottom']], zone):
            uncertain = True
    if uncertain or recognized > 1:
        return reject('Existing or ambiguous marks in carrier signature area; no overlay')
    if recognized == 1:
        result.update(status='ALREADY_SIGNED', reason='One exact approved embedded signature in verified area' if embedded else 'One exact approved stamp appearance in verified signature area')
        return result, None
    result.update(status='SIGNED', reason='Carrier INN and normalized legal name match; signature anchors and geometry confirmed; area has no known marks')
    return result, rect


def process(input_path, config_path, output_dir, inspector=inspect_page, max_pages=None):
    input_path, config_path, output_dir = Path(input_path), Path(config_path), Path(output_dir)
    cfg = json.loads(config_path.read_text(encoding='utf-8-sig'))
    cfg['facsimile'] = str((config_path.parent / cfg['facsimile']).resolve())
    # Validate facsimile before processing; its content is never logged.
    with Image.open(cfg['facsimile']) as im:
        im.verify()
    original = input_path.read_bytes()
    source_hash = hashlib.sha256(original).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / (input_path.stem+'_SIGNED.pdf')
    logs = [target.with_suffix('.json'), target.with_suffix('.txt')]
    if target.resolve() == input_path.resolve() or any(p.exists() for p in [target]+logs):
        raise FileExistsError('Output would overwrite source or existing results')
    reader = PdfReader(input_path)
    if max_pages is not None and len(reader.pages) > max_pages:
        raise ValueError('Page limit exceeded')
    if reader.is_encrypted:
        raise ValueError('Encrypted PDF requires manual review')
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    results = []
    with pdfplumber.open(input_path) as doc:
        for index, page in enumerate(writer.pages):
            backup = copy.deepcopy(page)
            row = dict(page=index+1, request_number=None, date=None, carrier_name_raw=None,
                       carrier_name_normalized=None, inn_found=None, inn_expected=cfg['inn'],
                       driver=None, vehicle=None, extraction_method='pdf_text_layer')
            try:
                decision, rect = inspector(page, doc.pages[index], cfg)
                row.update(decision)
                if rect is not None:
                    stamp = make_stamp(cfg['facsimile'], rect)
                    form = stamp['/AP']['/N']
                    objects = form['/Resources']['/XObject']
                    for name, obj in list(objects.items()):
                        if '/SMask' in obj:
                            obj[NameObject('/SMask')] = writer._add_object(obj['/SMask'])
                        objects[name] = writer._add_object(obj)
                    stamp['/AP'][NameObject('/N')] = writer._add_object(form)
                    writer.add_annotation(index, stamp)
                    row['placement_rect_pdf'] = rect
            except Exception as exc:
                page.clear()
                page.update(backup)
                row.update(status='ERROR', reason='Page processing failed: '+type(exc).__name__)
            results.append(row)
    if input_path.read_bytes() != original:
        raise RuntimeError('Input changed during processing')
    temporary = target.with_suffix('.pdf.tmp')
    try:
        with temporary.open('xb') as stream:
            writer.write(stream)
        check = PdfReader(temporary)
        if len(check.pages) != len(reader.pages):
            raise RuntimeError('Output page count changed')
        for a,b in zip(reader.pages,check.pages):
            if list(a.mediabox) != list(b.mediabox) or list(a.cropbox) != list(b.cropbox) or a.get('/Rotate',0) != b.get('/Rotate',0):
                raise RuntimeError('Output geometry changed')
        with pdfplumber.open(temporary) as rendered_doc:
            for index, row in enumerate(results):
                if row['status'] in ('SIGNED', 'ALREADY_SIGNED'):
                    verification, _ = inspect_page(check.pages[index], rendered_doc.pages[index], cfg)
                    if verification['status'] != 'ALREADY_SIGNED':
                        raise RuntimeError('Saved stamp appearance failed verification')
        report = dict(source_sha256=source_hash, output_sha256=hashlib.sha256(temporary.read_bytes()).hexdigest(), pages=results)
        logs[0].write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        logs[1].write_text('\n'.join(f"Page {r['page']} | {r['request_number']} | {r['status']} | {r['reason']}" for r in results),encoding='utf-8')
        temporary.rename(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', default='output/pdf', type=Path)
    parser.add_argument('--max-pages', type=int)
    args = parser.parse_args()
    try:
        path, rows = process(args.input,args.config,args.output,max_pages=args.max_pages)
        print(path)
        for row in rows:
            print(row['page'], row['status'], row['reason'])
        raise SystemExit(2 if any(r['status'] in ('ERROR','REVIEW_REQUIRED') for r in rows) else 0)
    except (OSError,ValueError) as exc:
        parser.exit(1,'Processing failed: '+type(exc).__name__+'\n')
