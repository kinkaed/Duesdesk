import base64
import io
import re
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from django.core import signing

DEFAULTS = {'primary':'#214f43', 'secondary':'#edf4e6', 'accent':'#527735'}


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        raise ValueError('Colors must be six-digit hex values, such as #214f43.')
    return value.lower()


def luminance(hex_color):
    channels = [int(hex_color[i:i+2],16)/255 for i in (1,3,5)]
    linear = [v/12.92 if v<=0.04045 else ((v+0.055)/1.055)**2.4 for v in channels]
    return sum(v*w for v,w in zip(linear, (0.2126,0.7152,0.0722)))


def text_color(background):
    # The better of black/white always achieves at least 4.5:1 for opaque sRGB.
    value = luminance(background)
    return '#000000' if (value+0.05)/0.05 >= 1.05/(value+0.05) else '#ffffff'


def branding_json(org):
    palette = {}
    for key,default in DEFAULTS.items():
        try:palette[key]=color(getattr(org,key))
        except (ValueError,AttributeError):palette[key]=default
    return {'id':org.pk,'public_id':str(org.public_id),'name':org.name,
            'logo_url':f'/organizations/{org.public_id}/logo/' if org.logo else '',
            **palette, **{f'{k}_text':text_color(v) for k,v in palette.items()}}


def decode_logo(upload):
    if not upload or upload.size > 1024*1024:
        raise ValueError('Choose a PNG, JPEG or WebP logo smaller than 1 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(upload)
            if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height > 4000000:
                raise ValueError('Use a PNG, JPEG or WebP with at most 4 million pixels.')
            image = ImageOps.exif_transpose(image)
            image.thumbnail((256,256))
            rgba = image.convert('RGBA')
            rgb = Image.new('RGB',rgba.size,'white')
            rgb.paste(rgba,mask=rgba.getchannel('A'))
            quantized = rgb.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
            raw = quantized.getpalette()
            ranked = sorted(quantized.getcolors(),reverse=True)
            colors = ['#%02x%02x%02x'%tuple(raw[i*3:i*3+3]) for count,i in ranked]
            prominent = [c for c in colors if max(int(c[i:i+2],16) for i in (1,3,5))-min(int(c[i:i+2],16) for i in (1,3,5)) > 25]
            primary = (prominent or colors)[0]
            accent = prominent[1] if len(prominent)>1 else primary
            secondary = '#%02x%02x%02x'%tuple(round(int(primary[i:i+2],16)*0.15+255*0.85) for i in (1,3,5))
            # Fresh image removes metadata; never serve uploaded bytes directly.
            clean = Image.new('RGBA',rgba.size)
            clean.paste(rgba)
            output=io.BytesIO();clean.save(output,format='PNG')
            return output.getvalue(), {'primary':primary,'secondary':secondary,'accent':accent}
    except (OSError,UnidentifiedImageError,Image.DecompressionBombWarning,Image.DecompressionBombError) as error:
        raise ValueError('This image could not be read safely. Choose another logo.') from error


def logo_token(raw, owner):
    return signing.dumps({'png':base64.b64encode(raw).decode(),'owner':owner},salt='logo-preview',compress=True)


def read_logo_token(token, owner):
    try:
        data=signing.loads(token,salt='logo-preview',max_age=1800)
        if data['owner'] != owner:raise ValueError()
        return base64.b64decode(data['png'],validate=True)
    except (signing.BadSignature,ValueError,KeyError):
        raise ValueError('Logo preview expired. Please upload it again.')
