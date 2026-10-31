"""企业微信回调加解密，按官方 WXBizMsgCrypt 算法实现（AES-256-CBC，PKCS#7 按 32 字节补位，SHA1 签名）。

明文结构：random(16B) + msg_len(4B 网络字节序) + msg + receiveid（自建应用为 corpid）。
加解密用 cryptography 库；没装时抛出 CryptoUnavailable（桌面版一般用不到收消息）。"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct


class CryptoUnavailable(RuntimeError):
    pass


class WeComCryptoError(ValueError):
    """签名不对、解密失败、receiveid 不符等。code 对应官方错误码。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


BLOCK = 32


def _cipher(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        raise CryptoUnavailable("没有安装 cryptography，不能处理企业微信回调") from exc
    return Cipher(algorithms.AES(key), modes.CBC(key[:16]))


def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    parts = sorted([str(token), str(timestamp), str(nonce), str(encrypt)])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


class WXBizMsgCrypt:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        if len(encoding_aes_key or "") != 43:
            raise WeComCryptoError(-40004, "EncodingAESKey 应为 43 位")
        try:
            self.key = base64.b64decode(encoding_aes_key + "=")
        except Exception as exc:
            raise WeComCryptoError(-40004, "EncodingAESKey 不是合法的 Base64") from exc
        if len(self.key) != 32:
            raise WeComCryptoError(-40004, "EncodingAESKey 解码后不是 32 字节")
        self.token = token
        self.receive_id = receive_id

    # ---- 底层 ----
    def encrypt(self, text: str, random16: bytes | None = None) -> str:
        msg = text.encode("utf-8")
        plain = (random16 or os.urandom(16)) + struct.pack("I", socket.htonl(len(msg))) + msg + self.receive_id.encode("utf-8")
        pad = BLOCK - len(plain) % BLOCK
        plain += bytes([pad]) * pad
        enc = _cipher(self.key).encryptor()
        return base64.b64encode(enc.update(plain) + enc.finalize()).decode("ascii")

    def decrypt(self, encrypted: str) -> str:
        try:
            raw = base64.b64decode(encrypted)
            dec = _cipher(self.key).decryptor()
            plain = dec.update(raw) + dec.finalize()
        except CryptoUnavailable:
            raise
        except Exception as exc:
            raise WeComCryptoError(-40007, "AES 解密失败") from exc
        pad = plain[-1]
        if pad < 1 or pad > BLOCK:
            pad = 0
        content = plain[16: len(plain) - pad]
        if len(content) < 4:
            raise WeComCryptoError(-40007, "明文长度不对")
        length = socket.ntohl(struct.unpack("I", content[:4])[0])
        msg, receive = content[4: 4 + length], content[4 + length:]
        if receive.decode("utf-8", "replace") != self.receive_id:
            raise WeComCryptoError(-40005, "receiveid（企业ID）不匹配")
        return msg.decode("utf-8")

    # ---- 官方接口 ----
    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        if signature(self.token, timestamp, nonce, echostr) != msg_signature:
            raise WeComCryptoError(-40001, "签名校验失败（Token 不对？）")
        return self.decrypt(echostr)

    def decrypt_msg(self, post_xml: str, msg_signature: str, timestamp: str, nonce: str) -> str:
        encrypt = extract(post_xml, "Encrypt")
        if not encrypt:
            raise WeComCryptoError(-40002, "XML 里没有 Encrypt")
        if signature(self.token, timestamp, nonce, encrypt) != msg_signature:
            raise WeComCryptoError(-40001, "签名校验失败（Token 不对？）")
        return self.decrypt(encrypt)

    def encrypt_msg(self, reply_xml: str, nonce: str, timestamp: str) -> str:
        encrypt = self.encrypt(reply_xml)
        sig = signature(self.token, timestamp, nonce, encrypt)
        return (f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt><MsgSignature><![CDATA[{sig}]]></MsgSignature>"
                f"<TimeStamp>{timestamp}</TimeStamp><Nonce><![CDATA[{nonce}]]></Nonce></xml>")


def extract(xml: str, tag: str) -> str:
    """取 XML 里某个标签的文本。用标准库解析，禁止外部实体（defusedxml 风格：只接受简单结构）。"""
    import xml.etree.ElementTree as ET

    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise WeComCryptoError(-40002, "XML 里不允许 DOCTYPE / ENTITY")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise WeComCryptoError(-40002, "XML 解析失败") from exc
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""
