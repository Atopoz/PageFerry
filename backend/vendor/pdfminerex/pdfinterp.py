import logging
import re
from io import BytesIO
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union, cast

from . import settings
from .casting import safe_cmyk, safe_float, safe_int, safe_matrix, safe_rgb
from .cmapdb import CMap, CMapBase, CMapDB
from .pdfcolor import PREDEFINED_COLORSPACE, PDFColorSpace
from .pdfdevice import PDFDevice, PDFTextSeq
from .pdfexceptions import PDFException, PDFValueError
from .pdffont import (
    PDFCIDFont,
    PDFFont,
    PDFFontError,
    PDFTrueTypeFont,
    PDFType1Font,
    PDFType3Font,
)
from .pdfpage import PDFPage
from .pdftypes import (
    LITERALS_ASCII85_DECODE,
    PDFObjRef,
    PDFStream,
    dict_value,
    list_value,
    resolve1,
    stream_value,
)
from .psexceptions import PSEOF, PSTypeError
from .psparser import (
    KWD,
    LIT,
    PSKeyword,
    PSLiteral,
    PSStackParser,
    PSStackType,
    keyword_name,
    literal_name,
)
from .utils import (
    MATRIX_IDENTITY,
    Matrix,
    PathSegment,
    Point,
    Rect,
    choplist,
    make_compat_str,
    mult_matrix,
    inverse_matrix,
    translate_matrix,
)

log = logging.getLogger(__name__)


class PDFResourceError(PDFException):
    pass


class PDFInterpreterError(PDFException):
    pass


LITERAL_PDF = LIT("PDF")
LITERAL_TEXT = LIT("Text")
LITERAL_FONT = LIT("Font")
LITERAL_FORM = LIT("Form")
LITERAL_IMAGE = LIT("Image")


class TextBlockRecorder:
    """Collects operators between BT and ET for tracing."""

    def __init__(
        self,
        ctm: Optional[Matrix] = None,
        fontid: Optional[str] = None,
        fontsize: Optional[float] = None,
        charspace: float = 0,
        wordspace: float = 0,
        scaling: float = 100,
        leading: float = 0,
        render: int = 0,
        rise: float = 0,
        ncolor: Optional["Color"] = None,
        ncs: Optional[PDFColorSpace] = None,
        scolor: Optional["Color"] = None,
        scs: Optional[PDFColorSpace] = None,
        text_matrix: Optional[Matrix] = None,
        text_show: Optional[Tuple[str, Tuple[object, ...]]] = None,
        xobject_path: Optional[Sequence[str]] = None,
        mode: str = "block",
    ) -> None:
        self._ops: List[Tuple[str, Tuple[object, ...]]] = []
        self._ctm = ctm
        self._fontid = fontid
        self._fontsize = fontsize
        self._charspace = charspace
        self._wordspace = wordspace
        self._scaling = scaling
        self._leading = leading
        self._render = render
        self._rise = rise
        self._ncolor = ncolor
        self._ncs = ncs
        self._scolor = scolor
        self._scs = scs
        self._text_matrix = text_matrix
        self._text_show = text_show
        self._xobject_path = tuple(xobject_path or ())
        self._mode = mode
        self._finalized = False
        self._cached: Dict[bool, str] = {}

    @classmethod
    def from_text_show(
        cls,
        *,
        ctm: Optional[Matrix],
        text_matrix: Matrix,
        operator: str,
        args: Sequence[object],
        fontid: Optional[str],
        fontsize: Optional[float],
        charspace: float,
        wordspace: float,
        scaling: float,
        leading: float,
        render: int,
        rise: float,
        ncolor: Optional["Color"],
        ncs: Optional[PDFColorSpace],
        scolor: Optional["Color"],
        scs: Optional[PDFColorSpace],
        xobject_path: Optional[Sequence[str]] = None,
    ) -> "TextBlockRecorder":
        return cls(
            ctm=ctm,
            fontid=fontid,
            fontsize=fontsize,
            charspace=charspace,
            wordspace=wordspace,
            scaling=scaling,
            leading=leading,
            render=render,
            rise=rise,
            ncolor=ncolor,
            ncs=ncs,
            scolor=scolor,
            scs=scs,
            text_matrix=text_matrix,
            text_show=(operator, tuple(args)),
            xobject_path=xobject_path,
            mode="text_show",
        )

    def add(self, operator: str, args: Sequence[object]) -> None:
        if self._finalized:
            return
        self._ops.append((operator, tuple(args)))
        self._cached.clear()

    def finalize(self) -> None:
        self._finalized = True

    def as_string(self, decode_bytes: bool = True) -> str:
        if decode_bytes not in self._cached:
            lines: List[str] = []

            if self._mode == "text_show":
                if not decode_bytes and self._ctm is not None:
                    a, b, c, d, e, f = self._ctm
                    lines.append(
                        f"q\n{a:.12g} {b:.12g} {c:.12g} {d:.12g} {e:.12g} {f:.12g} cm"
                    )

                if not decode_bytes:
                    self._append_color(lines, self._scolor, is_stroke=True)
                    self._append_color(lines, self._ncolor, is_stroke=False)

                lines.append("BT")
                if self._fontid is not None and self._fontsize is not None:
                    lines.append(f"/{self._fontid} {self._fontsize:.12g} Tf")
                if self._charspace != 0:
                    lines.append(f"{self._charspace:.12g} Tc")
                if self._wordspace != 0:
                    lines.append(f"{self._wordspace:.12g} Tw")
                if self._scaling != 100:
                    lines.append(f"{self._scaling:.12g} Tz")
                if self._leading != 0:
                    lines.append(f"{self._leading:.12g} TL")
                if self._render != 0:
                    lines.append(f"{self._render} Tr")
                if self._rise != 0:
                    lines.append(f"{self._rise:.12g} Ts")
                if self._text_matrix is not None:
                    a, b, c, d, e, f = self._text_matrix
                    lines.append(
                        f"{a:.12g} {b:.12g} {c:.12g} {d:.12g} {e:.12g} {f:.12g} Tm"
                    )

                if self._text_show is not None:
                    name, args = self._text_show
                    if args:
                        arg_str = " ".join(
                            TextBlockRecorder._format_arg(arg, decode_bytes) for arg in args
                        )
                        lines.append(f"{arg_str} {name}")
                    else:
                        lines.append(name)

                lines.append("ET")
                if not decode_bytes and self._ctm is not None:
                    lines.append("Q")
            else:
                # 增加 CTM 环境恢复指令 (Absolute PDF CTM)
                if not decode_bytes and self._ctm is not None:
                    a, b, c, d, e, f = self._ctm
                    # 关键修复：确保指令前后有清晰的换行，防止浏览器解析粘连
                    lines.append(
                        f"q\n{a:.12g} {b:.12g} {c:.12g} {d:.12g} {e:.12g} {f:.12g} cm"
                    )

                # 注入捕获到的图形状态 (颜色)
                if not decode_bytes:
                    self._append_color(lines, self._ncolor, is_stroke=False)

                for name, args in self._ops:
                    if args:
                        arg_str = " ".join(
                            TextBlockRecorder._format_arg(arg, decode_bytes) for arg in args
                        )
                        # PDF 语法是后缀表达式：参数在前，操作符在后
                        lines.append(f"{arg_str} {name}")
                    else:
                        lines.append(name)

                    # 在 BT 之后立即注入捕捉到的完整文本状态，确保环境完整
                    if name == "BT" and not decode_bytes:
                        if self._fontid is not None and self._fontsize is not None:
                            lines.append(f"/{self._fontid} {self._fontsize:.12g} Tf")
                        if self._charspace != 0:
                            lines.append(f"{self._charspace:.12g} Tc")
                        if self._wordspace != 0:
                            lines.append(f"{self._wordspace:.12g} Tw")
                        if self._scaling != 100:
                            lines.append(f"{self._scaling:.12g} Tz")
                        if self._leading != 0:
                            lines.append(f"{self._leading:.12g} TL")
                        if self._render != 0:
                            lines.append(f"{self._render} Tr")
                        if self._rise != 0:
                            lines.append(f"{self._rise:.12g} Ts")

                if not decode_bytes and self._ctm is not None:
                    lines.append("Q")

            # 使用换行符连接，这是 PDF 指令流最安全的物理隔离方式
            self._cached[decode_bytes] = "\n".join(lines)
        return self._cached[decode_bytes]

    def as_pdf_source(self) -> str:
        return self.as_string(decode_bytes=False)

    def as_pdf_bytes(self) -> bytes:
        return self.as_pdf_source().encode("latin-1")

    @property
    def xobject_path(self) -> Tuple[str, ...]:
        return self._xobject_path

    @staticmethod
    def _append_color(lines: List[str], color: Optional["Color"], *, is_stroke: bool) -> None:
        if color is None:
            return
        if isinstance(color, (list, tuple)):
            color_vals = " ".join(str(x) for x in color)
            if len(color) == 1:
                lines.append(f"{color_vals} {'G' if is_stroke else 'g'}")
            elif len(color) == 3:
                lines.append(f"{color_vals} {'RG' if is_stroke else 'rg'}")
            elif len(color) == 4:
                lines.append(f"{color_vals} {'K' if is_stroke else 'k'}")
            return
        lines.append(f"{color} {'G' if is_stroke else 'g'}")

    @staticmethod
    def _format_arg(arg: object, decode_bytes: bool) -> str:
        if isinstance(arg, bytes):
            if decode_bytes:
                try:
                    return f"({make_compat_str(arg)})"
                except Exception:
                    return TextBlockRecorder._encode_hex_string(arg)
            # 关键：在生成 PDF 源码时，字节流务必使用 HEX 格式，以确保 CID 字体正确识别
            return TextBlockRecorder._encode_hex_string(arg)
        if isinstance(arg, (list, tuple)):
            inner = " ".join(TextBlockRecorder._format_arg(x, decode_bytes) for x in arg)
            return f"[{inner}]"
        if isinstance(arg, bool):
            return "true" if arg else "false"
        if isinstance(arg, PSLiteral):
            return "/" + literal_name(arg)
        if isinstance(arg, (int, float)):
            return TextBlockRecorder._format_number(arg)
        if arg is None:
            return "null"
        return repr(arg)

    @staticmethod
    def _encode_hex_string(data: bytes) -> str:
        return "<" + data.hex() + ">"

    @staticmethod
    def _format_number(value: Union[int, float]) -> str:
        if isinstance(value, int):
            return str(value)
        if value.is_integer():
            return str(int(value))
        return f"{value:.12g}"


class PDFTextState:
    matrix: Matrix
    linematrix: Point

    def __init__(self) -> None:
        self.font: Optional[PDFFont] = None
        self.fontid: Optional[str] = None
        self.fontsize: float = 0
        self.charspace: float = 0
        self.wordspace: float = 0
        self.scaling: float = 100
        self.leading: float = 0
        self.render: int = 0
        self.rise: float = 0
        self.text_block: Optional[TextBlockRecorder] = None
        self.reset()
        # self.matrix is set
        # self.linematrix is set

    def __repr__(self) -> str:
        return (
            "<PDFTextState: font=%r, fontid=%s, fontsize=%r, charspace=%r, "
            "wordspace=%r, scaling=%r, leading=%r, render=%r, rise=%r, "
            "matrix=%r, linematrix=%r>"
            % (
                self.font,
                self.fontid,
                self.fontsize,
                self.charspace,
                self.wordspace,
                self.scaling,
                self.leading,
                self.render,
                self.rise,
                self.matrix,
                self.linematrix,
            )
        )

    def copy(self) -> "PDFTextState":
        obj = PDFTextState()
        obj.font = self.font
        obj.fontid = self.fontid
        obj.fontsize = self.fontsize
        obj.charspace = self.charspace
        obj.wordspace = self.wordspace
        obj.scaling = self.scaling
        obj.leading = self.leading
        obj.render = self.render
        obj.rise = self.rise
        obj.matrix = self.matrix
        obj.linematrix = self.linematrix
        obj.text_block = self.text_block
        return obj

    def reset(self) -> None:
        self.matrix = MATRIX_IDENTITY
        self.linematrix = (0, 0)
        self.text_block = None


Color = Union[
    float,  # Greyscale
    Tuple[float, float, float],  # R, G, B
    Tuple[float, float, float, float],  # C, M, Y, K
]


class PDFGraphicState:
    def __init__(self) -> None:
        self.linewidth: float = 0
        self.linecap: Optional[object] = None
        self.linejoin: Optional[object] = None
        self.miterlimit: Optional[object] = None
        self.dash: Optional[Tuple[object, object]] = None
        self.intent: Optional[object] = None
        self.flatness: Optional[object] = None
        self.salpha: float = 1.0
        self.nalpha: float = 1.0

        # stroking color
        self.scolor: Color = 0
        self.scs: PDFColorSpace = PREDEFINED_COLORSPACE["DeviceGray"]

        # non stroking color
        self.ncolor: Color = 0
        self.ncs: PDFColorSpace = PREDEFINED_COLORSPACE["DeviceGray"]

    def copy(self) -> "PDFGraphicState":
        obj = PDFGraphicState()
        obj.linewidth = self.linewidth
        obj.linecap = self.linecap
        obj.linejoin = self.linejoin
        obj.miterlimit = self.miterlimit
        obj.dash = self.dash
        obj.intent = self.intent
        obj.flatness = self.flatness
        obj.salpha = self.salpha
        obj.nalpha = self.nalpha
        obj.scolor = self.scolor
        obj.scs = self.scs
        obj.ncolor = self.ncolor
        obj.ncs = self.ncs
        return obj

    def __repr__(self) -> str:
        return (
            "<PDFGraphicState: linewidth=%r, linecap=%r, linejoin=%r, "
            " miterlimit=%r, dash=%r, intent=%r, flatness=%r, "
            " stroking color=%r, non stroking color=%r, stroking alpha=%r, "
            " non stroking alpha=%r>"
            % (
                self.linewidth,
                self.linecap,
                self.linejoin,
                self.miterlimit,
                self.dash,
                self.intent,
                self.flatness,
                self.scolor,
                self.ncolor,
                self.salpha,
                self.nalpha,
            )
        )


class PDFResourceManager:
    """Repository of shared resources.

    ResourceManager facilitates reuse of shared resources
    such as fonts and images so that large objects are not
    allocated multiple times.
    """

    def __init__(self, caching: bool = True) -> None:
        self.caching = caching
        self._cached_fonts: Dict[object, PDFFont] = {}

    def get_procset(self, procs: Sequence[object]) -> None:
        for proc in procs:
            if proc is LITERAL_PDF or proc is LITERAL_TEXT:
                pass
            else:
                pass

    def get_cmap(self, cmapname: str, strict: bool = False) -> CMapBase:
        try:
            return CMapDB.get_cmap(cmapname)
        except CMapDB.CMapNotFound:
            if strict:
                raise
            return CMap()

    def get_font(self, objid: object, spec: Mapping[str, object]) -> PDFFont:
        if objid and objid in self._cached_fonts:
            font = self._cached_fonts[objid]
        else:
            log.debug("get_font: create: objid=%r, spec=%r", objid, spec)
            if settings.STRICT:
                if spec["Type"] is not LITERAL_FONT:
                    raise PDFFontError("Type is not /Font")
            # Create a Font object.
            if "Subtype" in spec:
                subtype = literal_name(spec["Subtype"])
            else:
                if settings.STRICT:
                    raise PDFFontError("Font Subtype is not specified.")
                subtype = "Type1"
            if subtype in ("Type1", "MMType1"):
                # Type1 Font
                font = PDFType1Font(self, spec)
            elif subtype == "TrueType":
                # TrueType Font
                font = PDFTrueTypeFont(self, spec)
            elif subtype == "Type3":
                # Type3 Font
                font = PDFType3Font(self, spec)
            elif subtype in ("CIDFontType0", "CIDFontType2"):
                # CID Font
                font = PDFCIDFont(self, spec)
            elif subtype == "Type0":
                # Type0 Font
                dfonts = list_value(spec["DescendantFonts"])
                assert dfonts
                subspec = dict_value(dfonts[0]).copy()
                for k in ("Encoding", "ToUnicode"):
                    if k in spec:
                        subspec[k] = resolve1(spec[k])
                font = self.get_font(None, subspec)
            else:
                if settings.STRICT:
                    raise PDFFontError("Invalid Font spec: %r" % spec)
                font = PDFType1Font(self, spec)  # this is so wrong!
            if objid and self.caching:
                self._cached_fonts[objid] = font
        return font


class PDFContentParser(PSStackParser[Union[PSKeyword, PDFStream]]):
    def __init__(self, streams: Sequence[object]) -> None:
        self.streams = streams
        self.istream = 0
        # PSStackParser.__init__(fp=None) is safe only because we've overloaded
        # all the methods that would attempt to access self.fp without first
        # calling self.fillfp().
        PSStackParser.__init__(self, None)  # type: ignore[arg-type]

    def fillfp(self) -> None:
        if not self.fp:
            if self.istream < len(self.streams):
                strm = stream_value(self.streams[self.istream])
                self.istream += 1
            else:
                raise PSEOF("Unexpected EOF, file truncated?")
            self.fp = BytesIO(strm.get_data())

    def seek(self, pos: int) -> None:
        self.fillfp()
        PSStackParser.seek(self, pos)

    def fillbuf(self) -> None:
        if self.charpos < len(self.buf):
            return
        while 1:
            self.fillfp()
            self.bufpos = self.fp.tell()
            self.buf = self.fp.read(self.BUFSIZ)
            if self.buf:
                break
            self.fp = None  # type: ignore[assignment]
        self.charpos = 0

    def get_inline_data(self, pos: int, target: bytes = b"EI") -> Tuple[int, bytes]:
        self.seek(pos)
        i = 0
        data = b""
        while i <= len(target):
            self.fillbuf()
            if i:
                ci = self.buf[self.charpos]
                c = bytes((ci,))
                data += c
                self.charpos += 1
                if (
                    len(target) <= i
                    and c.isspace()
                    or i < len(target)
                    and c == (bytes((target[i],)))
                ):
                    i += 1
                else:
                    i = 0
            else:
                try:
                    j = self.buf.index(target[0], self.charpos)
                    data += self.buf[self.charpos : j + 1]
                    self.charpos = j + 1
                    i = 1
                except ValueError:
                    data += self.buf[self.charpos :]
                    self.charpos = len(self.buf)
        data = data[: -(len(target) + 1)]  # strip the last part
        data = re.sub(rb"(\x0d\x0a|[\x0d\x0a])$", b"", data)
        return (pos, data)

    def flush(self) -> None:
        self.add_results(*self.popall())

    KEYWORD_BI = KWD(b"BI")
    KEYWORD_ID = KWD(b"ID")
    KEYWORD_EI = KWD(b"EI")

    def do_keyword(self, pos: int, token: PSKeyword) -> None:
        if token is self.KEYWORD_BI:
            # inline image within a content stream
            self.start_type(pos, "inline")
        elif token is self.KEYWORD_ID:
            try:
                (_, objs) = self.end_type("inline")
                if len(objs) % 2 != 0:
                    error_msg = f"Invalid dictionary construct: {objs!r}"
                    raise PSTypeError(error_msg)
                d = {literal_name(k): resolve1(v) for (k, v) in choplist(2, objs)}
                eos = b"EI"
                filter = d.get("F", None)
                if filter is not None:
                    if isinstance(filter, PSLiteral):
                        filter = [filter]
                    if filter[0] in LITERALS_ASCII85_DECODE:
                        eos = b"~>"
                (pos, data) = self.get_inline_data(pos + len(b"ID "), target=eos)
                if eos != b"EI":  # it may be necessary for decoding
                    data += eos
                obj = PDFStream(d, data)
                self.push((pos, obj))
                if eos == b"EI":  # otherwise it is still in the stream
                    self.push((pos, self.KEYWORD_EI))
            except PSTypeError:
                if settings.STRICT:
                    raise
        else:
            self.push((pos, token))


PDFStackT = PSStackType[PDFStream]
"""Types that may appear on the PDF argument stack."""


class PDFPageInterpreter:
    """Processor for the content of a PDF page

    Reference: PDF Reference, Appendix A, Operator Summary
    """

    def __init__(self, rsrcmgr: PDFResourceManager, device: PDFDevice) -> None:
        self.rsrcmgr = rsrcmgr
        self.device = device
        self._active_text_block: Optional[TextBlockRecorder] = None
        self._xobject_stack: List[str] = []

    def dup(self) -> "PDFPageInterpreter":
        interpreter = self.__class__(self.rsrcmgr, self.device)
        interpreter._xobject_stack = list(self._xobject_stack)
        return interpreter

    def init_resources(self, resources: Dict[object, object]) -> None:
        """Prepare the fonts and XObjects listed in the Resource attribute."""
        self.resources = resources
        self.fontmap: Dict[object, PDFFont] = {}
        self.xobjmap = {}
        self.extgstatemap = {}
        self.csmap: Dict[str, PDFColorSpace] = PREDEFINED_COLORSPACE.copy()
        if not resources:
            return

        def get_colorspace(spec: object) -> Optional[PDFColorSpace]:
            if isinstance(spec, list):
                name = literal_name(spec[0])
            else:
                name = literal_name(spec)
            if name == "ICCBased" and isinstance(spec, list) and len(spec) >= 2:
                return PDFColorSpace(name, stream_value(spec[1])["N"])
            elif name == "Separation" and isinstance(spec, list) and len(spec) >= 4:
                alternate_spec = resolve1(spec[2])
                alternate = get_colorspace(alternate_spec)
                return PDFColorSpace(
                    name,
                    1,
                    alternate=alternate,
                    tint_transform=resolve1(spec[3]),
                )
            elif name == "DeviceN" and isinstance(spec, list) and len(spec) >= 2:
                return PDFColorSpace(name, len(list_value(spec[1])))
            else:
                return PREDEFINED_COLORSPACE.get(name)

        for k, v in dict_value(resources).items():
            log.debug("Resource: %r: %r", k, v)
            if k == "Font":
                for fontid, spec in dict_value(v).items():
                    objid = None
                    if isinstance(spec, PDFObjRef):
                        objid = spec.objid
                    spec = dict_value(spec)
                    self.fontmap[fontid] = self.rsrcmgr.get_font(objid, spec)
            elif k == "ColorSpace":
                for csid, spec in dict_value(v).items():
                    colorspace = get_colorspace(resolve1(spec))
                    if colorspace is not None:
                        self.csmap[csid] = colorspace
            elif k == "ProcSet":
                self.rsrcmgr.get_procset(list_value(v))
            elif k == "XObject":
                for xobjid, xobjstrm in dict_value(v).items():
                    self.xobjmap[xobjid] = xobjstrm
            elif k == "ExtGState":
                for gstateid, spec in dict_value(v).items():
                    self.extgstatemap[gstateid] = spec

    def init_state(self, ctm: Matrix) -> None:
        """Initialize the text and graphic states for rendering a page."""
        # gstack: stack for graphical states.
        self.gstack: List[Tuple[Matrix, Matrix, PDFTextState, PDFGraphicState]] = []
        self.ctm = ctm
        self.pdf_ctm = MATRIX_IDENTITY  # 追踪原始 PDF 物理坐标系
        self.initial_ctm = ctm
        self.inv_initial_ctm = inverse_matrix(ctm)
        self.device.set_ctm(self.ctm)
        self.textstate = PDFTextState()
        self.graphicstate = PDFGraphicState()
        self.curpath: List[PathSegment] = []
        # argstack: stack for command arguments.
        self.argstack: List[PDFStackT] = []
        self._active_text_block = None

    def push(self, obj: PDFStackT) -> None:
        self.argstack.append(obj)

    def pop(self, n: int) -> List[PDFStackT]:
        if n == 0:
            return []
        x = self.argstack[-n:]
        self.argstack = self.argstack[:-n]
        return x

    def get_current_state(self) -> Tuple[Matrix, Matrix, PDFTextState, PDFGraphicState]:
        return (self.ctm, self.pdf_ctm, self.textstate.copy(), self.graphicstate.copy())

    def set_current_state(
        self,
        state: Tuple[Matrix, Matrix, PDFTextState, PDFGraphicState],
    ) -> None:
        (self.ctm, self.pdf_ctm, self.textstate, self.graphicstate) = state
        self.device.set_ctm(self.ctm)
        self._active_text_block = self.textstate.text_block

    def _record_text_block_operator(
        self,
        name: str,
        args: Sequence[PDFStackT],
    ) -> None:
        if name in {"BT", "ET"}:
            self._active_text_block = None
            self.textstate.text_block = None

    def do_q(self) -> None:
        """Save graphics state"""
        self.gstack.append(self.get_current_state())

    def do_Q(self) -> None:
        """Restore graphics state"""
        if self.gstack:
            self.set_current_state(self.gstack.pop())

    def do_cm(
        self,
        a1: PDFStackT,
        b1: PDFStackT,
        c1: PDFStackT,
        d1: PDFStackT,
        e1: PDFStackT,
        f1: PDFStackT,
    ) -> None:
        """Concatenate matrix to current transformation matrix"""
        matrix = safe_matrix(a1, b1, c1, d1, e1, f1)

        if matrix is None:
            log.debug(
                f"Cannot concatenate matrix to current transformation matrix because not all values in {(a1, b1, c1, d1, e1, f1)!r} can be parsed as floats"
            )
        else:
            self.ctm = mult_matrix(matrix, self.ctm)
            self.pdf_ctm = mult_matrix(matrix, self.pdf_ctm)  # 同步追踪 PDF 指令流的变换
            self.device.set_ctm(self.ctm)

    def do_w(self, linewidth: PDFStackT) -> None:
        """Set line width"""
        linewidth_f = safe_float(linewidth)
        if linewidth_f is None:
            log.debug(
                f"Cannot set line width because {linewidth!r} is an invalid float value"
            )
        else:
            self.graphicstate.linewidth = linewidth_f

    def do_J(self, linecap: PDFStackT) -> None:
        """Set line cap style"""
        self.graphicstate.linecap = linecap

    def do_j(self, linejoin: PDFStackT) -> None:
        """Set line join style"""
        self.graphicstate.linejoin = linejoin

    def do_M(self, miterlimit: PDFStackT) -> None:
        """Set miter limit"""
        self.graphicstate.miterlimit = miterlimit

    def do_d(self, dash: PDFStackT, phase: PDFStackT) -> None:
        """Set line dash pattern"""
        self.graphicstate.dash = (dash, phase)

    def do_ri(self, intent: PDFStackT) -> None:
        """Set color rendering intent"""
        self.graphicstate.intent = intent

    def do_i(self, flatness: PDFStackT) -> None:
        """Set flatness tolerance"""
        self.graphicstate.flatness = flatness

    def do_gs(self, name: PDFStackT) -> None:
        """Set parameters from graphics state parameter dictionary"""
        gstateid = literal_name(name)
        try:
            parameters = dict_value(self.extgstatemap[gstateid])
        except KeyError:
            if settings.STRICT:
                raise PDFInterpreterError("Undefined ExtGState id: %r" % gstateid)
            return

        for key, attribute in (("CA", "salpha"), ("ca", "nalpha")):
            if key not in parameters:
                continue
            alpha = safe_float(resolve1(parameters[key]))
            if alpha is not None:
                setattr(self.graphicstate, attribute, min(max(alpha, 0.0), 1.0))

    def do_m(self, x: PDFStackT, y: PDFStackT) -> None:
        """Begin new subpath"""
        x_f = safe_float(x)
        y_f = safe_float(y)

        if x_f is None or y_f is None:
            point = ("m", x, y)
            log.debug(
                f"Cannot start new subpath because not all values in {point!r} can be parsed as floats"
            )
        else:
            point = ("m", x_f, y_f)
            self.curpath.append(point)

    def do_l(self, x: PDFStackT, y: PDFStackT) -> None:
        """Append straight line segment to path"""
        x_f = safe_float(x)
        y_f = safe_float(y)
        if x_f is None or y_f is None:
            point = ("l", x, y)
            log.debug(
                f"Cannot append straight line segment to path because not all values in {point!r} can be parsed as floats"
            )
        else:
            point = ("l", x_f, y_f)
            self.curpath.append(point)

    def do_c(
        self,
        x1: PDFStackT,
        y1: PDFStackT,
        x2: PDFStackT,
        y2: PDFStackT,
        x3: PDFStackT,
        y3: PDFStackT,
    ) -> None:
        """Append curved segment to path (three control points)"""
        x1_f = safe_float(x1)
        y1_f = safe_float(y1)
        x2_f = safe_float(x2)
        y2_f = safe_float(y2)
        x3_f = safe_float(x3)
        y3_f = safe_float(y3)
        if (
            x1_f is None
            or y1_f is None
            or x2_f is None
            or y2_f is None
            or x3_f is None
            or y3_f is None
        ):
            point = ("c", x1, y1, x2, y2, x3, y3)
            log.debug(
                f"Cannot append curved segment to path because not all values in {point!r} can be parsed as floats"
            )
        else:
            point = ("c", x1_f, y1_f, x2_f, y2_f, x3_f, y3_f)
            self.curpath.append(point)

    def do_v(self, x2: PDFStackT, y2: PDFStackT, x3: PDFStackT, y3: PDFStackT) -> None:
        """Append curved segment to path (initial point replicated)"""
        x2_f = safe_float(x2)
        y2_f = safe_float(y2)
        x3_f = safe_float(x3)
        y3_f = safe_float(y3)
        if x2_f is None or y2_f is None or x3_f is None or y3_f is None:
            point = ("v", x2, y2, x3, y3)
            log.debug(
                f"Cannot append curved segment to path because not all values in {point!r} can be parsed as floats"
            )
        else:
            point = ("v", x2_f, y2_f, x3_f, y3_f)
            self.curpath.append(point)

    def do_y(self, x1: PDFStackT, y1: PDFStackT, x3: PDFStackT, y3: PDFStackT) -> None:
        """Append curved segment to path (final point replicated)"""
        x1_f = safe_float(x1)
        y1_f = safe_float(y1)
        x3_f = safe_float(x3)
        y3_f = safe_float(y3)
        if x1_f is None or y1_f is None or x3_f is None or y3_f is None:
            point = ("y", x1, y1, x3, y3)
            log.debug(
                f"Cannot append curved segment to path because not all values in {point!r} can be parsed as floats"
            )
        else:
            point = ("y", x1_f, y1_f, x3_f, y3_f)
            self.curpath.append(point)

    def do_h(self) -> None:
        """Close subpath"""
        self.curpath.append(("h",))

    def do_re(self, x: PDFStackT, y: PDFStackT, w: PDFStackT, h: PDFStackT) -> None:
        """Append rectangle to path"""
        x_f = safe_float(x)
        y_f = safe_float(y)
        w_f = safe_float(w)
        h_f = safe_float(h)

        if x_f is None or y_f is None or w_f is None or h_f is None:
            values = (x, y, w, h)
            log.debug(
                f"Cannot append rectangle to path because not all values in {values!r} can be parsed as floats"
            )
        else:
            self.curpath.append(("m", x_f, y_f))
            self.curpath.append(("l", x_f + w_f, y_f))
            self.curpath.append(("l", x_f + w_f, y_f + h_f))
            self.curpath.append(("l", x_f, y_f + h_f))
            self.curpath.append(("h",))

    def do_S(self) -> None:
        """Stroke path"""
        self.device.paint_path(self.graphicstate, True, False, False, self.curpath)
        self.curpath = []

    def do_s(self) -> None:
        """Close and stroke path"""
        self.do_h()
        self.do_S()

    def do_f(self) -> None:
        """Fill path using nonzero winding number rule"""
        self.device.paint_path(self.graphicstate, False, True, False, self.curpath)
        self.curpath = []

    def do_F(self) -> None:
        """Fill path using nonzero winding number rule (obsolete)"""

    def do_f_a(self) -> None:
        """Fill path using even-odd rule"""
        self.device.paint_path(self.graphicstate, False, True, True, self.curpath)
        self.curpath = []

    def do_B(self) -> None:
        """Fill and stroke path using nonzero winding number rule"""
        self.device.paint_path(self.graphicstate, True, True, False, self.curpath)
        self.curpath = []

    def do_B_a(self) -> None:
        """Fill and stroke path using even-odd rule"""
        self.device.paint_path(self.graphicstate, True, True, True, self.curpath)
        self.curpath = []

    def do_b(self) -> None:
        """Close, fill, and stroke path using nonzero winding number rule"""
        self.do_h()
        self.do_B()

    def do_b_a(self) -> None:
        """Close, fill, and stroke path using even-odd rule"""
        self.do_h()
        self.do_B_a()

    def do_n(self) -> None:
        """End path without filling or stroking"""
        self.curpath = []

    def do_W(self) -> None:
        """Set clipping path using nonzero winding number rule"""

    def do_W_a(self) -> None:
        """Set clipping path using even-odd rule"""

    def do_CS(self, name: PDFStackT) -> None:
        """Set color space for stroking operations

        Introduced in PDF 1.1
        """
        try:
            self.graphicstate.scs = self.csmap[literal_name(name)]
        except KeyError:
            if settings.STRICT:
                raise PDFInterpreterError("Undefined ColorSpace: %r" % name)

    def do_cs(self, name: PDFStackT) -> None:
        """Set color space for nonstroking operations"""
        try:
            self.graphicstate.ncs = self.csmap[literal_name(name)]
        except KeyError:
            if settings.STRICT:
                raise PDFInterpreterError("Undefined ColorSpace: %r" % name)

    def do_G(self, gray: PDFStackT) -> None:
        """Set gray level for stroking operations"""
        gray_f = safe_float(gray)

        if gray_f is None:
            log.debug(
                f"Cannot set gray level because {gray!r} is an invalid float value"
            )
        else:
            self.graphicstate.scolor = gray_f
            self.graphicstate.scs = self.csmap["DeviceGray"]

    def do_g(self, gray: PDFStackT) -> None:
        """Set gray level for nonstroking operations"""
        gray_f = safe_float(gray)

        if gray_f is None:
            log.debug(
                f"Cannot set gray level because {gray!r} is an invalid float value"
            )
        else:
            self.graphicstate.ncolor = gray_f
            self.graphicstate.ncs = self.csmap["DeviceGray"]

    def do_RG(self, r: PDFStackT, g: PDFStackT, b: PDFStackT) -> None:
        """Set RGB color for stroking operations"""
        rgb = safe_rgb(r, g, b)

        if rgb is None:
            log.debug(
                f"Cannot set RGB stroke color because not all values in {(r, g, b)!r} can be parsed as floats"
            )
        else:
            self.graphicstate.scolor = rgb
            self.graphicstate.scs = self.csmap["DeviceRGB"]

    def do_rg(self, r: PDFStackT, g: PDFStackT, b: PDFStackT) -> None:
        """Set RGB color for nonstroking operations"""
        rgb = safe_rgb(r, g, b)

        if rgb is None:
            log.debug(
                f"Cannot set RGB non-stroke color because not all values in {(r, g, b)!r} can be parsed as floats"
            )
        else:
            self.graphicstate.ncolor = rgb
            self.graphicstate.ncs = self.csmap["DeviceRGB"]

    def do_K(self, c: PDFStackT, m: PDFStackT, y: PDFStackT, k: PDFStackT) -> None:
        """Set CMYK color for stroking operations"""
        cmyk = safe_cmyk(c, m, y, k)

        if cmyk is None:
            log.debug(
                f"Cannot set CMYK stroke color because not all values in {(c, m, y, k)!r} can be parsed as floats"
            )
        else:
            self.graphicstate.scolor = cmyk
            self.graphicstate.scs = self.csmap["DeviceCMYK"]

    def do_k(self, c: PDFStackT, m: PDFStackT, y: PDFStackT, k: PDFStackT) -> None:
        """Set CMYK color for nonstroking operations"""
        cmyk = safe_cmyk(c, m, y, k)

        if cmyk is None:
            log.debug(
                f"Cannot set CMYK non-stroke color because not all values in {(c, m, y, k)!r} can be parsed as floats"
            )
        else:
            self.graphicstate.ncolor = cmyk
            self.graphicstate.ncs = self.csmap["DeviceCMYK"]

    def do_SCN(self) -> None:
        """Set color for stroking operations."""
        n = self.graphicstate.scs.ncomponents

        components = self.pop(n)
        if len(components) != n:
            log.debug(
                f"Cannot set stroke color because expected {n} components but got {components:!r}"
            )

        elif len(components) == 1:
            gray = components[0]
            gray_f = safe_float(gray)
            if gray_f is None:
                log.debug(
                    f"Cannot set gray stroke color because {gray!r} is an invalid float value"
                )
            else:
                self.graphicstate.scolor = gray_f

        elif len(components) == 3:
            rgb = safe_rgb(*components)

            if rgb is None:
                log.debug(
                    f"Cannot set RGB stroke color because components {components!r} cannot be parsed as RGB"
                )
            else:
                self.graphicstate.scolor = rgb

        elif len(components) == 4:
            cmyk = safe_cmyk(*components)

            if cmyk is None:
                log.debug(
                    f"Cannot set CMYK stroke color because components {components!r} cannot be parsed as CMYK"
                )
            else:
                self.graphicstate.scolor = cmyk

        else:
            log.debug(
                f"Cannot set stroke color because {len(components)} components are specified but only 1 (grayscale), 3 (rgb) and 4 (cmyk) are supported"
            )

    def do_scn(self) -> None:
        """Set color for nonstroking operations"""
        n = self.graphicstate.ncs.ncomponents

        components = self.pop(n)
        if len(components) != n:
            log.debug(
                f"Cannot set non-stroke color because expected {n} components but got {components:!r}"
            )

        elif len(components) == 1:
            gray = components[0]
            gray_f = safe_float(gray)
            if gray_f is None:
                log.debug(
                    f"Cannot set gray non-stroke color because {gray!r} is an invalid float value"
                )
            else:
                self.graphicstate.ncolor = gray_f

        elif len(components) == 3:
            rgb = safe_rgb(*components)

            if rgb is None:
                log.debug(
                    f"Cannot set RGB non-stroke color because components {components!r} cannot be parsed as RGB"
                )
            else:
                self.graphicstate.ncolor = rgb

        elif len(components) == 4:
            cmyk = safe_cmyk(*components)

            if cmyk is None:
                log.debug(
                    f"Cannot set CMYK non-stroke color because components {components!r} cannot be parsed as CMYK"
                )
            else:
                self.graphicstate.ncolor = cmyk

        else:
            log.debug(
                f"Cannot set non-stroke color because {len(components)} components are specified but only 1 (grayscale), 3 (rgb) and 4 (cmyk) are supported"
            )

    def do_SC(self) -> None:
        """Set color for stroking operations"""
        self.do_SCN()

    def do_sc(self) -> None:
        """Set color for nonstroking operations"""
        self.do_scn()

    def do_sh(self, name: object) -> None:
        """Paint area defined by shading pattern"""

    def do_BT(self) -> None:
        """Begin text object

        Initializing the text matrix, Tm, and the text line matrix, Tlm, to
        the identity matrix. Text objects cannot be nested; a second BT cannot
        appear before an ET.
        """
        self.textstate.reset()
        self.textstate.text_block = self._active_text_block

    def do_ET(self) -> None:
        """End a text object"""
        self.textstate.text_block = None

    def do_BX(self) -> None:
        """Begin compatibility section"""

    def do_EX(self) -> None:
        """End compatibility section"""

    def do_MP(self, tag: PDFStackT) -> None:
        """Define marked-content point"""
        if isinstance(tag, PSLiteral):
            self.device.do_tag(tag)
        else:
            log.debug(
                f"Cannot define marked-content point because {tag!r} is not a PSLiteral"
            )

    def do_DP(self, tag: PDFStackT, props: PDFStackT) -> None:
        """Define marked-content point with property list"""
        if isinstance(tag, PSLiteral):
            self.device.do_tag(tag, props)
        else:
            log.debug(
                f"Cannot define marked-content point with property list because {tag!r} is not a PSLiteral"
            )

    def do_BMC(self, tag: PDFStackT) -> None:
        """Begin marked-content sequence"""
        if isinstance(tag, PSLiteral):
            self.device.begin_tag(tag)
        else:
            log.debug(
                f"Cannot begin marked-content sequence because {tag!r} is not a PSLiteral"
            )

    def do_BDC(self, tag: PDFStackT, props: PDFStackT) -> None:
        """Begin marked-content sequence with property list"""
        if isinstance(tag, PSLiteral):
            self.device.begin_tag(tag, props)
        else:
            log.debug(
                f"Cannot begin marked-content sequence with property list because {tag!r} is not a PSLiteral"
            )

    def do_EMC(self) -> None:
        """End marked-content sequence"""
        self.device.end_tag()

    def do_Tc(self, space: PDFStackT) -> None:
        """Set character spacing.

        Character spacing is used by the Tj, TJ, and ' operators.

        :param space: a number expressed in unscaled text space units.
        """
        charspace = safe_float(space)
        if charspace is None:
            log.debug(
                f"Could not set character spacing because {space!r} is an invalid float value"
            )
        else:
            self.textstate.charspace = charspace

    def do_Tw(self, space: PDFStackT) -> None:
        """Set the word spacing.

        Word spacing is used by the Tj, TJ, and ' operators.

        :param space: a number expressed in unscaled text space units
        """
        wordspace = safe_float(space)
        if wordspace is None:
            log.debug(
                f"Could not set word spacing becuase {space!r} is an invalid float value"
            )
        else:
            self.textstate.wordspace = wordspace

    def do_Tz(self, scale: PDFStackT) -> None:
        """Set the horizontal scaling.

        :param scale: is a number specifying the percentage of the normal width
        """
        scale_f = safe_float(scale)

        if scale_f is None:
            log.debug(
                f"Could not set horizontal scaling because {scale!r} is an invalid float value"
            )
        else:
            self.textstate.scaling = scale_f

    def do_TL(self, leading: PDFStackT) -> None:
        """Set the text leading.

        Text leading is used only by the T*, ', and " operators.

        :param leading: a number expressed in unscaled text space units
        """
        leading_f = safe_float(leading)
        if leading_f is None:
            log.debug(
                f"Could not set text leading because {leading!r} is an invalid float value"
            )
        else:
            self.textstate.leading = -leading_f

    def do_Tf(self, fontid: PDFStackT, fontsize: PDFStackT) -> None:
        """Set the text font

        :param fontid: the name of a font resource in the Font subdictionary
            of the current resource dictionary
        :param fontsize: size is a number representing a scale factor.
        """
        self.textstate.fontid = literal_name(fontid)
        try:
            self.textstate.font = self.fontmap[self.textstate.fontid]
        except KeyError:
            if settings.STRICT:
                raise PDFInterpreterError("Undefined Font id: %r" % fontid)
            self.textstate.font = self.rsrcmgr.get_font(None, {})

        fontsize_f = safe_float(fontsize)
        if fontsize_f is None:
            log.debug(
                f"Could not set text font because {fontsize!r} is an invalid float value"
            )
        else:
            self.textstate.fontsize = fontsize_f

    def do_Tr(self, render: PDFStackT) -> None:
        """Set the text rendering mode"""
        render_i = safe_int(render)

        if render_i is None:
            log.debug(
                f"Could not set text rendering mode because {render!r} is an invalid int value"
            )
        else:
            self.textstate.render = render_i

    def do_Ts(self, rise: PDFStackT) -> None:
        """Set the text rise

        :param rise: a number expressed in unscaled text space units
        """
        rise_f = safe_float(rise)

        if rise_f is None:
            log.debug(
                f"Could not set text rise because {rise!r} is an invalid float value"
            )
        else:
            self.textstate.rise = rise_f

    def do_Td(self, tx: PDFStackT, ty: PDFStackT) -> None:
        """Move to the start of the next line

        Offset from the start of the current line by (tx , ty).
        """
        tx_ = safe_float(tx)
        ty_ = safe_float(ty)
        if tx_ is not None and ty_ is not None:
            (a, b, c, d, e, f) = self.textstate.matrix
            e_new = tx_ * a + ty_ * c + e
            f_new = tx_ * b + ty_ * d + f
            self.textstate.matrix = (a, b, c, d, e_new, f_new)

        elif settings.STRICT:
            raise PDFValueError(f"Invalid offset ({tx!r}, {ty!r}) for Td")

        self.textstate.linematrix = (0, 0)

    def do_TD(self, tx: PDFStackT, ty: PDFStackT) -> None:
        """Move to the start of the next line.

        offset from the start of the current line by (tx , ty). As a side effect, this
        operator sets the leading parameter in the text state.
        """
        tx_ = safe_float(tx)
        ty_ = safe_float(ty)

        if tx_ is not None and ty_ is not None:
            (a, b, c, d, e, f) = self.textstate.matrix
            e_new = tx_ * a + ty_ * c + e
            f_new = tx_ * b + ty_ * d + f
            self.textstate.matrix = (a, b, c, d, e_new, f_new)

        elif settings.STRICT:
            raise PDFValueError("Invalid offset ({tx}, {ty}) for TD")

        if ty_ is not None:
            self.textstate.leading = ty_

        self.textstate.linematrix = (0, 0)

    def do_Tm(
        self,
        a: PDFStackT,
        b: PDFStackT,
        c: PDFStackT,
        d: PDFStackT,
        e: PDFStackT,
        f: PDFStackT,
    ) -> None:
        """Set text matrix and text line matrix"""
        values = (a, b, c, d, e, f)
        matrix = safe_matrix(*values)

        if matrix is None:
            log.debug(
                f"Could not set text matrix because not all values in {values!r} can be parsed as floats"
            )
        else:
            self.textstate.matrix = matrix
            self.textstate.linematrix = (0, 0)

    def do_T_a(self) -> None:
        """Move to start of next text line"""
        (a, b, c, d, e, f) = self.textstate.matrix
        self.textstate.matrix = (
            a,
            b,
            c,
            d,
            self.textstate.leading * c + e,
            self.textstate.leading * d + f,
        )
        self.textstate.linematrix = (0, 0)

    def do_TJ(self, seq: PDFStackT) -> None:
        """Show text, allowing individual glyph positioning"""
        if self.textstate.font is None:
            if settings.STRICT:
                raise PDFInterpreterError("No font specified!")
            return
        render_seq = cast(PDFTextSeq, seq)
        text_matrix = translate_matrix(self.textstate.matrix, self.textstate.linematrix)
        recorder = TextBlockRecorder.from_text_show(
            ctm=self.pdf_ctm,
            text_matrix=text_matrix,
            operator="TJ",
            args=(render_seq,),
            fontid=self.textstate.fontid,
            fontsize=self.textstate.fontsize,
            charspace=self.textstate.charspace,
            wordspace=self.textstate.wordspace,
            scaling=self.textstate.scaling,
            leading=self.textstate.leading,
            render=self.textstate.render,
            rise=self.textstate.rise,
            ncolor=self.graphicstate.ncolor,
            ncs=self.graphicstate.ncs,
            scolor=self.graphicstate.scolor,
            scs=self.graphicstate.scs,
            xobject_path=self._xobject_stack,
        )
        previous_text_block = self.textstate.text_block
        self.textstate.text_block = recorder
        self.device.render_string(
            self.textstate,
            render_seq,
            self.graphicstate.ncs,
            self.graphicstate.copy(),
        )
        self.textstate.text_block = previous_text_block

    def do_Tj(self, s: PDFStackT) -> None:
        """Show text"""
        if self.textstate.font is None:
            if settings.STRICT:
                raise PDFInterpreterError("No font specified!")
            return
        text_matrix = translate_matrix(self.textstate.matrix, self.textstate.linematrix)
        recorder = TextBlockRecorder.from_text_show(
            ctm=self.pdf_ctm,
            text_matrix=text_matrix,
            operator="Tj",
            args=(s,),
            fontid=self.textstate.fontid,
            fontsize=self.textstate.fontsize,
            charspace=self.textstate.charspace,
            wordspace=self.textstate.wordspace,
            scaling=self.textstate.scaling,
            leading=self.textstate.leading,
            render=self.textstate.render,
            rise=self.textstate.rise,
            ncolor=self.graphicstate.ncolor,
            ncs=self.graphicstate.ncs,
            scolor=self.graphicstate.scolor,
            scs=self.graphicstate.scs,
            xobject_path=self._xobject_stack,
        )
        previous_text_block = self.textstate.text_block
        self.textstate.text_block = recorder
        self.device.render_string(
            self.textstate,
            cast(PDFTextSeq, [s]),
            self.graphicstate.ncs,
            self.graphicstate.copy(),
        )
        self.textstate.text_block = previous_text_block

    def do__q(self, s: PDFStackT) -> None:
        """Move to next line and show text

        The ' (single quote) operator.
        """
        self.do_T_a()
        self.do_TJ([s])

    def do__w(self, aw: PDFStackT, ac: PDFStackT, s: PDFStackT) -> None:
        """Set word and character spacing, move to next line, and show text

        The " (double quote) operator.
        """
        self.do_Tw(aw)
        self.do_Tc(ac)
        self.do_TJ([s])

    def do_BI(self) -> None:
        """Begin inline image object"""

    def do_ID(self) -> None:
        """Begin inline image data"""

    def do_EI(self, obj: PDFStackT) -> None:
        """End inline image object"""
        if isinstance(obj, PDFStream) and "W" in obj and "H" in obj:
            iobjid = str(id(obj))
            self.device.begin_figure(iobjid, (0, 0, 1, 1), MATRIX_IDENTITY)
            self.device.render_image(iobjid, obj)
            self.device.end_figure(iobjid)

    def do_Do(self, xobjid_arg: PDFStackT) -> None:
        """Invoke named XObject"""
        xobjid = literal_name(xobjid_arg)
        try:
            xobj = stream_value(self.xobjmap[xobjid])
        except KeyError:
            if settings.STRICT:
                raise PDFInterpreterError("Undefined xobject id: %r" % xobjid)
            return
        log.debug("Processing xobj: %r", xobj)
        subtype = xobj.get("Subtype")
        if subtype is LITERAL_FORM and "BBox" in xobj:
            interpreter = self.dup()
            interpreter._xobject_stack = [*self._xobject_stack, xobjid]
            bbox = cast(Rect, list_value(xobj["BBox"]))
            matrix = cast(Matrix, list_value(xobj.get("Matrix", MATRIX_IDENTITY)))
            # According to PDF reference 1.7 section 4.9.1, XObjects in
            # earlier PDFs (prior to v1.2) use the page's Resources entry
            # instead of having their own Resources entry.
            xobjres = xobj.get("Resources")
            if xobjres:
                resources = dict_value(xobjres)
            else:
                resources = self.resources.copy()
            self.device.begin_figure(xobjid, bbox, matrix)
            interpreter.render_contents(
                resources,
                [xobj],
                ctm=mult_matrix(matrix, self.ctm),
            )
            self.device.end_figure(xobjid)
        elif subtype is LITERAL_IMAGE and "Width" in xobj and "Height" in xobj:
            self.device.begin_figure(xobjid, (0, 0, 1, 1), MATRIX_IDENTITY)
            self.device.render_image(xobjid, xobj)
            self.device.end_figure(xobjid)
        else:
            # unsupported xobject type.
            pass

    def process_page(self, page: PDFPage) -> None:
        log.debug("Processing page: %r", page)
        (x0, y0, x1, y1) = page.mediabox
        if page.rotate == 90:
            ctm = (0, -1, 1, 0, -y0, x1)
        elif page.rotate == 180:
            ctm = (-1, 0, 0, -1, x1, y1)
        elif page.rotate == 270:
            ctm = (0, 1, -1, 0, y1, -x0)
        else:
            ctm = (1, 0, 0, 1, -x0, -y0)
        self.device.begin_page(page, ctm)
        self.render_contents(page.resources, page.contents, ctm=ctm)
        self.device.end_page(page)

    def render_contents(
        self,
        resources: Dict[object, object],
        streams: Sequence[object],
        ctm: Matrix = MATRIX_IDENTITY,
    ) -> None:
        """Render the content streams.

        This method may be called recursively.
        """
        log.debug(
            "render_contents: resources=%r, streams=%r, ctm=%r",
            resources,
            streams,
            ctm,
        )
        self.init_resources(resources)
        self.init_state(ctm)
        self.execute(list_value(streams))

    def execute(self, streams: Sequence[object]) -> None:
        try:
            parser = PDFContentParser(streams)
        except PSEOF:
            # empty page
            return
        while True:
            try:
                (_, obj) = parser.nextobject()
            except PSEOF:
                break
            if isinstance(obj, PSKeyword):
                name = keyword_name(obj)
                method = "do_%s" % name.replace("*", "_a").replace('"', "_w").replace(
                    "'",
                    "_q",
                )
                if hasattr(self, method):
                    func = getattr(self, method)
                    nargs = func.__code__.co_argcount - 1
                    if nargs:
                        args = self.pop(nargs)
                        log.debug("exec: %s %r", name, args)
                        if len(args) == nargs:
                            self._record_text_block_operator(name, args)
                            func(*args)
                    else:
                        log.debug("exec: %s", name)
                        self._record_text_block_operator(name, ())
                        func()
                elif settings.STRICT:
                    error_msg = "Unknown operator: %r" % name
                    raise PDFInterpreterError(error_msg)
            else:
                self.push(obj)
