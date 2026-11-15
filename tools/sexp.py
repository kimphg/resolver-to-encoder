import re
def tokenize(s):
    i=0;n=len(s)
    while i<n:
        c=s[i]
        if c in ' \t\r\n': i+=1
        elif c=='(' or c==')': yield c; i+=1
        elif c=='"':
            j=i+1; buf=[]
            while s[j]!='"':
                if s[j]=='\\': buf.append(s[j:j+2]); j+=2
                else: buf.append(s[j]); j+=1
            yield ('S',''.join(buf)); i=j+1
        else:
            j=i
            while j<n and s[j] not in ' \t\r\n()': j+=1
            yield s[i:j]; i=j
def parse(s):
    st=[[]]
    for t in tokenize(s):
        if t=='(': st.append([])
        elif t==')': x=st.pop(); st[-1].append(x)
        else: st[-1].append(t)
    return st[0]
def dump(x,ind=0):
    if isinstance(x,list):
        if all(not isinstance(e,list) for e in x):
            return '('+' '.join(dump(e) for e in x)+')'
        out='('; first=True
        for e in x:
            if isinstance(e,list): out+='\n'+'  '*(ind+1)+dump(e,ind+1)
            else: out+=('' if first else ' ')+dump(e)
            first=False
        return out+')'
    if isinstance(x,tuple): return '"'+x[1]+'"'
    return str(x)
def S(v): return ('S',v)
_cache={}
def lib(name):
    if name not in _cache:
        _cache[name]=parse(open(f'/usr/share/kicad/symbols/{name}.kicad_sym').read())[0]
    return _cache[name]
def find_sym(libname,sym):
    for e in lib(libname):
        if isinstance(e,list) and e[0]=='symbol' and e[1]==S(sym): return e
    raise KeyError(sym)
def child(e,tag):
    return [c for c in e if isinstance(c,list) and c and c[0]==tag]
def prop(e,k):
    for p in child(e,'property'):
        if p[1]==S(k): return p[2][1]
def flatten(libname,sym):
    import copy
    e=copy.deepcopy(find_sym(libname,sym))
    ext=child(e,'extends')
    if ext:
        par=flatten(libname,ext[0][1][1])
        # parent body with child's name and props
        new=[ 'symbol', S(sym)]+[c for c in par[2:] if not (isinstance(c,list) and c[0] in('property','symbol'))]
        new+= child(e,'property')
        for u in child(par,'symbol'):
            u=copy.deepcopy(u); u[1]=S(u[1][1].replace(par[1][1],sym,1)); new.append(u)
        e=new
    return e
def pins(e):
    out=[]
    def walk(x):
        for c in x:
            if isinstance(c,list):
                if c[0]=='pin':
                    at=child(c,'at')[0]; nm=child(c,'name')[0][1][1]; nu=child(c,'number')[0][1][1]
                    out.append(dict(type=c[1],x=float(at[1]),y=float(at[2]),a=int(float(at[3])),name=nm,num=nu,hidden='hide' in c))
                else: walk(c)
    walk(e); return out
