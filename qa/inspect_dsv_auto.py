"""Read code objects only. Never import/evaluate the original application."""
import dis,hashlib,marshal,struct,types,zlib,sys
from pathlib import Path

sys.stdout=Path(sys.argv[1]).open('x',encoding='utf-8')
source=Path(r'C:\Users\xyc\Desktop\trading\trading.exe').read_bytes()
cookie=source.rfind(b'MEI\014\013\012\013\016')
magic,size,toc_offset,toc_size,version,library=struct.unpack('!8sIIII64s',source[cookie:cookie+88])
start=cookie+88-size;toc=source[start+toc_offset:start+toc_offset+toc_size];pos=0;gui=None
while pos<len(toc):
    length,offset,compressed,raw_size,flag,kind=struct.unpack('!IIIIBc',toc[pos:pos+18])
    name=toc[pos+18:pos+length].split(b'\0')[0].decode('utf-8')
    if name=='gui':
        gui=source[start+offset:start+offset+compressed]
        if flag:gui=zlib.decompress(gui)
        assert len(gui)==raw_size
    pos+=length
assert gui is not None
print('GUI_SHA256',hashlib.sha256(gui).hexdigest(),'PYTHON',version)
root=marshal.loads(gui)
def walk(code):
    yield code
    for const in code.co_consts:
        if isinstance(const,types.CodeType):yield from walk(const)
terms=('offset','calc_one','completed_only','base_series','get_kline_serial','on_auto_calc','on_auto_apply')
for code in walk(root):
    names=[name for name in code.co_names if any(t in name for t in terms)]
    if names or code.co_qualname.startswith('StrategyThread._run.<locals>'):
        print('INDEX',code.co_qualname,code.co_firstlineno,names)
print('DETAILS')
for code in walk(root):
    if code.co_qualname in {'App.on_start','StrategyThread._run'}:
        instructions=list(dis.get_instructions(code));selected=set()
        for i,item in enumerate(instructions):
            if isinstance(item.argval,str) and any(t in item.argval for t in terms):selected.update(range(max(0,i-10),min(len(instructions),i+16)))
            if code.co_qualname=='App.on_start' or code.co_qualname=='StrategyThread._run' and item.positions.lineno is not None and (4400<=item.positions.lineno<=4460 or 4870<=item.positions.lineno<=5130):selected.add(i)
        print('FUNCTION',code.co_qualname)
        for i in sorted(selected):
            item=instructions[i];print(item.positions.lineno,item.offset,item.opname,repr(item.argval) if item.opname=='LOAD_SMALL_INT' else item.argrepr)
    elif code.co_qualname.startswith('App.on_auto_calc') or code.co_qualname.startswith('StrategyThread._run.<locals>') and any(t in code.co_name for t in ('auto','calc','daily','param','kline')):
        print('FUNCTION',code.co_qualname)
        for item in dis.get_instructions(code):print(item.positions.lineno,item.offset,item.opname,repr(item.argval) if item.opname=='LOAD_SMALL_INT' else item.argrepr)
