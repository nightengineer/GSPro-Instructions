#!/usr/bin/env python3
"""Bounded-memory decoder for our OpenSSL streaming CMS AuthEnvelopedData format.
Uses cryptography's RSA/AES-GCM primitives; fails closed for other CMS layouts.
"""
# CHANGEABLE: input archive, private PEM key and NEW output path are positional arguments.
import sys,io,os,getpass,warnings
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes

def read_exact(f,n):
 b=f.read(n)
 if len(b)!=n:raise ValueError('Truncated CMS')
 return b
def header(f):
 tag=read_exact(f,1)[0];n=read_exact(f,1)[0]
 if tag&31==31:raise ValueError('Unsupported high tag')
 if n==128:return tag,None
 if n&128:
  width=n&127
  if width>8:raise ValueError('Invalid length')
  n=int.from_bytes(read_exact(f,width),'big')
 return tag,n
def container(f,tag):
 t,n=header(f)
 if t!=tag or n is not None:raise ValueError('Expected streaming CMS container')
def value(f,tag,max_len=16384):
 t,n=header(f)
 if t!=tag or n is None or n>max_len:raise ValueError('Unexpected CMS field')
 return read_exact(f,n)
def end(f):
 if header(f)!=(0,0):raise ValueError('Unexpected trailing CMS field')
def oid(f,expected):
 if value(f,6).hex()!=expected:raise ValueError('Unsupported CMS algorithm')

def decrypt(source,keyfile,destination):
 out=Path(destination);partial=out.with_name(out.name+'.partial')
 if out.exists() or partial.exists():raise FileExistsError('Output must be new')
 os.umask(0o077)
 with open(source,'rb') as f:
  container(f,0x30);oid(f,'2a864886f70d0109100117');container(f,0xa0);container(f,0x30)
  if value(f,2)!=b'\0':raise ValueError('Unsupported AuthEnvelopedData version')
  recipients=io.BytesIO(value(f,0x31));recipient=io.BytesIO(value(recipients,0x30))
  if recipients.read(1):raise ValueError('Expected one recipient')
  if value(recipient,2)!=b'\0':raise ValueError('Unsupported recipient version')
  value(recipient,0x30) # issuerAndSerialNumber
  rsa=io.BytesIO(value(recipient,0x30));oid(rsa,'2a864886f70d010101')
  if value(rsa,5)!=b'' or rsa.read(1):raise ValueError('Unexpected RSA parameters')
  wrapped=value(recipient,4)
  if recipient.read(1):raise ValueError('Unexpected recipient field')
  key_bytes=Path(keyfile).read_bytes()
  password=None
  if b'BEGIN ENCRYPTED PRIVATE KEY' in key_bytes:
   with warnings.catch_warnings():
    warnings.simplefilter('error',getpass.GetPassWarning)
    password=getpass.getpass('Пароль защищённого ключа: ').encode()
  private=load_pem_private_key(key_bytes,password=password)
  key=private.decrypt(wrapped,padding.PKCS1v15())
  if len(key)!=32:raise ValueError('Invalid AES-256 key')
  container(f,0x30);oid(f,'2a864886f70d010701')
  algorithm=io.BytesIO(value(f,0x30));oid(algorithm,'60864801650304012e')
  params=io.BytesIO(value(algorithm,0x30));nonce=value(params,4)
  tag_len=int.from_bytes(value(params,2),'big')
  if len(nonce)!=12 or tag_len!=16 or params.read(1) or algorithm.read(1):raise ValueError('Unexpected GCM parameters')
  container(f,0xa0)
  cipher=Cipher(algorithms.AES(key),modes.GCM(nonce)).decryptor()
  try:
   with partial.open('xb') as dest:
    while True:
     t,n=header(f)
     if (t,n)==(0,0):break
     if t!=4 or n is None or n>1048576:raise ValueError('Unexpected encrypted chunk')
     dest.write(cipher.update(read_exact(f,n)))
    end(f) # encryptedContentInfo
    tag=value(f,4)
    if len(tag)!=tag_len:raise ValueError('Invalid authentication tag')
    dest.write(cipher.finalize_with_tag(tag))
    end(f);end(f);end(f)
    if f.read(1):raise ValueError('Trailing archive data')
    dest.flush();os.fsync(dest.fileno())
   partial.rename(out)
  except BaseException:
   partial.unlink(missing_ok=True)
   raise
if __name__=='__main__':
 if len(sys.argv)!=4:raise SystemExit('Usage: decrypt-cms-stream.py archive.cms private.pem NEW-output.tar.gz')
 decrypt(*sys.argv[1:]);print('CMS AES-256-GCM authentication passed')
