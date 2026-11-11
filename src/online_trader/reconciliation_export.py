"""Bounded display exports for a verified, non-trading fill comparison.

Exports never apply fees, alter fills or open an account. JSON is a whitelisted
display artifact; the original preview hash remains a source identity, not a new
signature over a redacted display copy.
"""

from io import BytesIO, StringIO
import csv
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
import zipfile

from online_trader.contracts.codec import canonical_bytes, content_hash
from online_trader.domain.values import decimal_value, identifier, sequence
from online_trader.errors import TraderError


MAX_EXPORT_BYTES=64*1024*1024
MAX_ROWS=100000
_STATUSES={'INCOMPLETE','INVALID_ROW','SCOPE_MISMATCH','DUPLICATE_EXACT','DUPLICATE_CONFLICT',
    'EXTERNAL_ONLY','AMBIGUOUS','CONFLICT','MATCHED_FEE_UNKNOWN','ALREADY_CONFIRMED','FEE_CONFLICT',
    'MATCHED_FEE_NEW','MATCHED_ESTIMATED_REPLACEMENT','LOCAL_ONLY'}
_META=('account_key','environment','trading_day','currency','source_account','source_sha256',
       'statement_hash','checkpoint_event_seq','ledger_boot_id','ledger_facts_hash','preview_hash')
_TOP=('schema_version',)+_META+('blocking',)
_STATEMENT=('account_key','environment','trading_day','currency','trade_key','contract_id','side',
            'effect','quantity','price','fee','time')
_ROW=('row_number','status','issues')+_STATEMENT+('local_trade_key','candidate_trade_keys',
    'local_price','local_quantity','local_fee','local_fee_quality','fee_delta')
_LOCAL=('trade_key','local_trade_key','fill_id','child_order_id','position_cycle_id','contract_id',
    'direction','side','effect','quantity','price','multiplier','trading_day','time','observed_at_utc',
    'fee','fee_quality','event_seq','status')
_ACTION=('trade_key','amount','quality','revision','evidence_id','fill_binding_hash','row_number')
_FEE=('amount','quality','revision','evidence_id')
_SUMMARY=('external_rows','local_rows','matched_rows','local_only_rows','external_only_rows',
          'duplicate_rows','blocking_rows','fee_action_count','total_fee_amount')
_ISSUE=('code','field','row_number','trade_key','local_trade_key','error_code')

_MAIN_COLUMNS=(('row_number','来源行'),('status','对照状态'),('issues','问题'),
    ('trade_key','外部成交身份'),('contract_id','外部合约'),('side','买卖'),('effect','开平'),
    ('quantity','数量'),('price','成交价'),('fee','手续费'),('time','成交时间'),
    ('local_trade_key','本地成交身份'),('local_price','本地成交价'),('local_quantity','本地数量'),
    ('local_fee','本地费用'),('local_fee_quality','本地费用质量'),('fee_delta','费用差额'),
    ('candidate_trade_keys','候选成交身份'))
_LOCAL_COLUMNS=(('trade_key','本地成交身份'),('contract_id','合约'),('direction','方向'),
    ('side','买卖'),('effect','开平'),('quantity','数量'),('price','成交价'),('fee','手续费'),
    ('fee_quality','费用质量'),('trading_day','交易日'),('observed_at_utc','观察时间UTC'),
    ('position_cycle_id','周期'),('status','缺失标记'))


def _error(code,message):
    raise TraderError('E_COMPARISON_EXPORT_'+code,message)


def _scalar(value):
    if value is not None and type(value) not in {str,int,bool}:
        _error('SCHEMA','Display columns must be explicit scalar values')
    return value


def _flat(row,fields):
    if type(row) is not dict:_error('SCHEMA','Expected a comparison display row')
    return {key:_scalar(row[key]) for key in fields if key in row}


def _list(value,name):
    if type(value) is not list or len(value)>MAX_ROWS:_error('LIMIT',name+' must be a bounded list')
    return value


def _issues(value):
    result=[]
    for issue in _list(value,'Comparison issues'):
        if type(issue) is str:result.append(issue)
        elif type(issue) is dict:result.append(_flat(issue,_ISSUE))
        else:_error('SCHEMA','Unsupported comparison issue')
    return result


def _material(preview):
    if type(preview) is not dict or preview.get('schema_version')!='FILL_RECONCILIATION/1':
        _error('SCHEMA','Unsupported comparison schema')
    for key in ('rows','local_only','actions'):_list(preview.get(key),key)
    raw=canonical_bytes(preview)
    if len(raw)>MAX_EXPORT_BYTES:_error('LIMIT','Comparison preview exceeds export capacity')
    sha=preview.get('preview_hash')
    if type(sha) is not str or not re.fullmatch('[0-9a-f]{64}',sha):_error('HASH','Missing canonical preview hash')
    if content_hash({key:value for key,value in preview.items() if key!='preview_hash'})!=sha:
        _error('HASH','Comparison content changed after preview verification')
    for key in ('source_sha256','statement_hash'):
        if type(preview.get(key)) is not str or not re.fullmatch('[0-9a-f]{64}',preview[key]):
            _error('SCHEMA','Expected a canonical comparison source hash')
    if 'ledger_facts_hash' in preview and (type(preview['ledger_facts_hash']) is not str or
            not re.fullmatch('[0-9a-f]{64}',preview['ledger_facts_hash'])):
        _error('SCHEMA','Expected a canonical ledger facts hash')
    if preview.get('ledger_boot_id') is not None:identifier(preview['ledger_boot_id'])
    identifier(preview.get('account_key'));sequence(preview.get('checkpoint_event_seq'))
    for key in ('environment','trading_day'):
        if type(preview.get(key)) is not str:_error('SCHEMA','Incomplete comparison scope')
    if type(preview.get('blocking')) is not bool:_error('SCHEMA','Comparison blocking must be explicit')
    material=_flat(preview,_TOP)
    rows=[]
    for row in preview['rows']:
        if type(row) is not dict:_error('SCHEMA','Expected a comparison row')
        nested=row.get('statement',{})
        if type(nested) is not dict:_error('SCHEMA','Expected normalized statement fields')
        merged=dict(row)
        for field in _STATEMENT:
            if field not in merged and field in nested:merged[field]=nested[field]
        result=_flat(merged,tuple(key for key in _ROW if key not in {'issues','candidate_trade_keys'}))
        if result.get('status') not in _STATUSES:_error('SCHEMA','Unknown comparison status')
        result['issues']=_issues(row.get('issues',[]))
        candidates=_list(row.get('candidate_trade_keys',[]),'Candidate identities')
        if any(type(key) is not str for key in candidates):_error('SCHEMA','Expected candidate identity strings')
        result['candidate_trade_keys']=list(candidates);rows.append(result)
    material['rows']=rows
    material['local_only']=[]
    for row in preview['local_only']:
        result=_flat(row,_LOCAL)
        result['status']='LOCAL_ONLY'
        if 'trade_key' not in result and result.get('local_trade_key') is not None:
            result['trade_key']=result['local_trade_key']
        material['local_only'].append(result)
    material['actions']=[]
    for row in preview['actions']:
        result=_flat(row,_ACTION)
        result['before_fee']=None if row.get('before_fee') is None else _flat(row['before_fee'],_FEE)
        material['actions'].append(result)
    summary=preview.get('summary')
    material['summary']=_flat(summary,_SUMMARY)
    status_counts=summary.get('by_status',{})
    if type(status_counts) is not dict:_error('SCHEMA','Expected comparison status counts')
    material['summary']['by_status']={key:_scalar(value) for key,value in status_counts.items() if key in _STATUSES}
    return material


def _text(value):
    if value is None:return ''
    if type(value) is bool:return 'true' if value else 'false'
    if type(value) in {list,dict}:return json.dumps(value,ensure_ascii=False,separators=(',',':'))
    return str(value)


def _csv_cell(value):
    text=_text(value)
    if type(value) is str:
        try:decimal_value(value);numeric=True
        except TraderError:numeric=False
        if not numeric and (text.startswith(('\t','\r','\n')) or text.lstrip(' \t\r\n').startswith(('=','+','-','@'))):
            return "'"+text
    return text


def _csv(material):
    columns=(*_META,'row_type','row_number','status','issues',
        *('statement_'+name for name in _STATEMENT),
        'local_trade_key','candidate_trade_keys','local_price','local_quantity','local_fee','local_fee_quality','fee_delta',
        'local_contract_id','local_direction','local_side','local_effect','local_time')
    stream=StringIO(newline='');writer=csv.writer(stream,lineterminator='\r\n');writer.writerow(columns)
    for row in material['rows']:
        values={key:material.get(key) for key in _META}
        values.update(row_type='EXTERNAL',row_number=row.get('row_number'),status=row.get('status'),issues=row['issues'])
        values.update({'statement_'+name:row.get(name) for name in _STATEMENT})
        for name in ('local_trade_key','candidate_trade_keys','local_price','local_quantity','local_fee','local_fee_quality','fee_delta'):
            values[name]=row.get(name)
        writer.writerow([_csv_cell(values.get(name)) for name in columns])
    for row in material['local_only']:
        values={key:material.get(key) for key in _META}
        values.update(row_type='LOCAL_ONLY',status='LOCAL_ONLY',issues=['LOCAL_NOT_IN_STATEMENT'],
            local_trade_key=row.get('trade_key'),local_price=row.get('price'),local_quantity=row.get('quantity'),
            local_fee=row.get('fee'),local_fee_quality=row.get('fee_quality'),local_contract_id=row.get('contract_id'),
            local_direction=row.get('direction'),local_side=row.get('side'),local_effect=row.get('effect'),
            local_time=row.get('time') or row.get('observed_at_utc'))
        writer.writerow([_csv_cell(values.get(name)) for name in columns])
    return stream.getvalue().encode('utf-8-sig')


_MAIN='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PKG='http://schemas.openxmlformats.org/package/2006/relationships'
ET.register_namespace('',_MAIN);ET.register_namespace('r',_REL)


def _xml(element):return ET.tostring(element,encoding='utf-8',xml_declaration=True).replace(b'\r',b'&#13;')


def _tag(name):return '{'+_MAIN+'}'+name


def _column(index):
    result=''
    while index:
        index,remainder=divmod(index-1,26);result=chr(65+remainder)+result
    return result


def _xml_text(value):
    text=_text(value)
    if any((ord(char)<32 and char not in '\t\r\n') or ord(char) in {0xfffe,0xffff} for char in text):
        _error('SCHEMA','A display cell contains an unsupported XML control character')
    return text


def _sheet(rows,widths):
    sheet=ET.Element(_tag('worksheet'))
    cols=max((len(row) for row in rows),default=1)
    ET.SubElement(sheet,_tag('dimension'),{'ref':'A1:'+_column(cols)+str(max(1,len(rows)))})
    views=ET.SubElement(sheet,_tag('sheetViews'));view=ET.SubElement(views,_tag('sheetView'),{'workbookViewId':'0'})
    ET.SubElement(view,_tag('pane'),{'ySplit':'1','topLeftCell':'A2','activePane':'bottomLeft','state':'frozen'})
    ET.SubElement(sheet,_tag('sheetFormatPr'),{'defaultRowHeight':'18'})
    columns=ET.SubElement(sheet,_tag('cols'))
    for index in range(1,cols+1):
        ET.SubElement(columns,_tag('col'),{'min':str(index),'max':str(index),
            'width':str(widths[index-1] if index<=len(widths) else 22),'customWidth':'1'})
    data=ET.SubElement(sheet,_tag('sheetData'))
    for row_index,values in enumerate(rows,1):
        row=ET.SubElement(data,_tag('row'),{'r':str(row_index)})
        for col,value in enumerate(values,1):
            if value is None:continue
            cell=ET.SubElement(row,_tag('c'),{'r':_column(col)+str(row_index),'t':'inlineStr','s':'1' if row_index==1 else '0'})
            inline=ET.SubElement(cell,_tag('is'));text=ET.SubElement(inline,_tag('t'),{'{http://www.w3.org/XML/1998/namespace}space':'preserve'})
            text.text=_xml_text(value)
    ET.SubElement(sheet,_tag('autoFilter'),{'ref':'A1:'+_column(cols)+str(max(1,len(rows)))})
    return _xml(sheet)


def _styles():
    style=ET.Element(_tag('styleSheet'));fonts=ET.SubElement(style,_tag('fonts'),{'count':'2'})
    for bold in (False,True):
        font=ET.SubElement(fonts,_tag('font'));ET.SubElement(font,_tag('sz'),{'val':'11'});ET.SubElement(font,_tag('name'),{'val':'Calibri'})
        if bold:ET.SubElement(font,_tag('b'))
    fills=ET.SubElement(style,_tag('fills'),{'count':'2'})
    for pattern in ('none','gray125'):ET.SubElement(ET.SubElement(fills,_tag('fill')),_tag('patternFill'),{'patternType':pattern})
    borders=ET.SubElement(style,_tag('borders'),{'count':'1'});border=ET.SubElement(borders,_tag('border'))
    for edge in ('left','right','top','bottom','diagonal'):ET.SubElement(border,_tag(edge))
    base=ET.SubElement(style,_tag('cellStyleXfs'),{'count':'1'});ET.SubElement(base,_tag('xf'),{'numFmtId':'0','fontId':'0','fillId':'0','borderId':'0'})
    cells=ET.SubElement(style,_tag('cellXfs'),{'count':'2'})
    for index in range(2):
        xf=ET.SubElement(cells,_tag('xf'),{'numFmtId':'0','fontId':str(index),'fillId':'0','borderId':'0','xfId':'0','applyAlignment':'1'})
        ET.SubElement(xf,_tag('alignment'),{'vertical':'center','wrapText':'1'})
    names=ET.SubElement(style,_tag('cellStyles'),{'count':'1'});ET.SubElement(names,_tag('cellStyle'),{'name':'Normal','xfId':'0','builtinId':'0'})
    return _xml(style)


def _xlsx(material):
    main=[[title for _,title in _MAIN_COLUMNS]]+[[row.get(key) for key,_ in _MAIN_COLUMNS] for row in material['rows']]
    local=[[title for _,title in _LOCAL_COLUMNS]]+[[row.get(key) for key,_ in _LOCAL_COLUMNS] for row in material['local_only']]
    overview=[['项目','值','成交身份','费用','质量','费用修订','费用证据','原费用','原费用质量','源行','绑定hash']]
    for name in ('schema_version',*_META,'blocking'):overview.append([name,material.get(name)])
    for name in _SUMMARY:
        if name in material['summary']:overview.append(['summary.'+name,material['summary'][name]])
    for name,count in material['summary']['by_status'].items():overview.append(['status.'+name,count])
    for action in material['actions']:
        before=action['before_fee'] or {}
        overview.append(['费用拟登记','仅预览；未写入',action.get('trade_key'),action.get('amount'),action.get('quality'),
            action.get('revision'),action.get('evidence_id'),before.get('amount'),before.get('quality'),
            action.get('row_number'),action.get('fill_binding_hash')])
    workbook=ET.Element(_tag('workbook'));views=ET.SubElement(workbook,_tag('bookViews'));ET.SubElement(views,_tag('workbookView'))
    sheets=ET.SubElement(workbook,_tag('sheets'))
    for index,name in enumerate(('成交对照','本地缺失','概览'),1):
        ET.SubElement(sheets,_tag('sheet'),{'name':name,'sheetId':str(index),'{'+_REL+'}id':'rId'+str(index)})
    rels=ET.Element('{'+_PKG+'}Relationships')
    for index in range(1,4):ET.SubElement(rels,'{'+_PKG+'}Relationship',{'Id':'rId'+str(index),'Type':_REL+'/worksheet','Target':'worksheets/sheet'+str(index)+'.xml'})
    ET.SubElement(rels,'{'+_PKG+'}Relationship',{'Id':'rId4','Type':_REL+'/styles','Target':'styles.xml'})
    package=ET.Element('{'+_PKG+'}Relationships');ET.SubElement(package,'{'+_PKG+'}Relationship',{'Id':'rId1','Type':_REL+'/officeDocument','Target':'xl/workbook.xml'})
    content_ns='http://schemas.openxmlformats.org/package/2006/content-types';types=ET.Element('{'+content_ns+'}Types')
    for extension,kind in (('rels','application/vnd.openxmlformats-package.relationships+xml'),('xml','application/xml')):
        ET.SubElement(types,'{'+content_ns+'}Default',{'Extension':extension,'ContentType':kind})
    for part,kind in (('workbook.xml','sheet.main'),('styles.xml','styles'),
                      ('worksheets/sheet1.xml','worksheet'),('worksheets/sheet2.xml','worksheet'),('worksheets/sheet3.xml','worksheet')):
        ET.SubElement(types,'{'+content_ns+'}Override',{'PartName':'/xl/'+part,
            'ContentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.'+kind+'+xml'})
    parts={'[Content_Types].xml':_xml(types),'_rels/.rels':_xml(package),'xl/workbook.xml':_xml(workbook),
        'xl/_rels/workbook.xml.rels':_xml(rels),'xl/styles.xml':_styles(),
        'xl/worksheets/sheet1.xml':_sheet(main,[10,30,40,34,18,10,10,10,24,24,26,34,24,12,24,20,24,42]),
        'xl/worksheets/sheet2.xml':_sheet(local,[34,18,12,12,12,10,24,24,20,16,28,36,20]),
        'xl/worksheets/sheet3.xml':_sheet(overview,[30,68,34,24,20,14,44,24,20,12,68])}
    if sum(map(len,parts.values()))>MAX_EXPORT_BYTES:_error('LIMIT','Uncompressed workbook exceeds export capacity')
    output=BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in parts.items():
            entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,data)
    return output.getvalue()


def export_comparison(preview,destination,format=None):
    """Export a verified display copy to a new file; never apply its actions."""
    try:path=Path(destination).resolve()
    except (TypeError,ValueError,OSError) as error:raise TraderError('E_COMPARISON_EXPORT_PATH','Invalid comparison destination') from error
    kind=path.suffix.lower().lstrip('.') if format is None else format.lower() if type(format) is str else None
    if kind not in {'csv','json','xlsx'}:_error('FORMAT','Expected CSV, JSON or XLSX export')
    if not path.parent.is_dir():_error('PATH','Comparison export directory does not exist')
    material=_material(preview)
    if kind=='csv':payload=_csv(material)
    elif kind=='xlsx':payload=_xlsx(material)
    else:
        material['source_schema_version']=material['schema_version']
        material['schema_version']='FILL_RECONCILIATION_EXPORT/1'
        material['export_schema']='FILL_RECONCILIATION_EXPORT/1'
        material['source_preview_hash']=material['preview_hash']
        payload=(json.dumps(material,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    if len(payload)>MAX_EXPORT_BYTES:_error('LIMIT','Serialized comparison exceeds export capacity')
    created=False
    try:
        with path.open('xb') as output:created=True;output.write(payload)
    except FileExistsError as error:raise TraderError('E_COMPARISON_EXPORT_EXISTS','Comparison destination already exists') from error
    except OSError as error:
        if created:
            try:path.unlink()
            except OSError:pass
        raise TraderError('E_COMPARISON_EXPORT_PATH','Comparison could not be written') from error
    return path
