import datetime
import hashlib
import hmac
import html
import json
import logging
import re
import time
from contextlib import contextmanager
from configparser import ConfigParser
from os import listdir, remove, environ, getcwd, chdir
from os.path import join, basename, dirname, realpath

import requests
import boto3
from bottle import abort, post, request, run
from git import Repo
from PIL import Image
from PIL.ExifTags import TAGS as EXIF_TAGS
from requests.exceptions import RequestException

uploader_dirpath = dirname(realpath(__file__))
rel = lambda f: join(uploader_dirpath, f)

@contextmanager
def pushd(where):
    start_dir = getcwd()
    chdir(where)
    yield
    chdir(start_dir)

mode = environ.get('MODE', 'prod')
config = ConfigParser()
config.read(rel('config.ini'))
config = config[mode]

DRY = environ.get('DRY')

logging.basicConfig(
    format = '[%(levelname)s] (%(name)s) %(message)s',
    level = logging.DEBUG if DRY else logging.INFO,
)

S3 = boto3.client('s3')
git = Repo(rel('blog')).git if mode != 'test' else None

ORIENTATIONS = [
    None,
    None,
    None,
    -180,
    None,
    None,
    -90,
    None,
    -270,
]

TEMP_PATH = '/tmp'

MAILGUN_AUTH = ( 'api', config['mailgun-key'] )


authorized_senders = re.compile(config['authorized-senders-pattern'])
def is_authorized():
    sender = request.forms.get('from')
    return authorized_senders.match(sender) is not None


cached_mailgun_token = None
def verify_mailgun_request(timestamp, token, signature):
    """
    Ensures that a webhook request from Mailgun is valid.
    Raises an exception if the request is invalid.
    """

    # Check to avoid reused tokens to prevent replay attacks.
    global cached_mailgun_token
    if token == cached_mailgun_token:
        raise ValueError('Mailgun token is identical to the previous one')
    cached_mailgun_token = token

    # Ensure that request timestamp is not older than 1 minute.
    if time.time() - int(timestamp) > 60:
        raise ValueError('Mailgun timestamp is older than 60 seconds')

    # Ensure that request signature matches up.
    api_key = bytes(config['mailgun-key'], 'utf-8')
    message = (timestamp + token).encode('utf-8')
    computed = hmac.new(api_key, message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, signature):
        raise ValueError('Computed signature does not match request signature')


def download_attachments(attachments):
    """
    Downloads a media attachment from Mailgun.

    Parameters
    ----------
    attachments: A string of attachments JSON data from a Mailgun request.

    Returns
    -------
    A tuple containing
    (1) A path to where the attachment is saved on disk.
    (2) A mimetype string for the attachment file.
    """

    # Attempt to parse the attachment from the request form.
    attachment = json.loads(attachments)[0]
    url = attachment['url']
    name = attachment['name']
    content_type = attachment['content-type']

    # Currently only images are valid.
    # TODO: Eventually, if other file types (or no files) are supported,
    # this check should be made more robust.
    if not content_type.startswith('image'):
        raise ValueError("Unsupported file type '%s'" % content_type)

    # SIDE EFFECT: Download the parsed attachment to a temporary location.
    save_path = join(TEMP_PATH, name)
    response = requests.get(url, auth = MAILGUN_AUTH, stream = True)
    response.raise_for_status()
    with open(save_path, 'wb') as f:
        for chunk in response:
            f.write(chunk)

    return save_path, content_type

def get_new_oid():
    posts = listdir(rel('blog/_posts'))
    sorted_oids = sorted([ int(p.split('.')[0].split('-')[-1]) for p in posts ])
    return sorted_oids[-1] + 1


def get_img_data(img):
    """
    Gets an image's EXIF metadata.

    Parameters
    ----------
    img: A PIL Image object.

    Returns
    -------
    A dictionary keyed by EXIF tags with their data as the values.
    """

    # Certain image files do not contain EXIF data, and `_getexif()` calls
    # raise an `AttributeError`. If this happens, just return an empty dict.
    try:
        img_exif = img._getexif()
        return {
            EXIF_TAGS[k]: v for k, v in img_exif.items() if k in EXIF_TAGS
        }
    except AttributeError:
        return {}


def delete(*paths):
    """
    Gathers its arguments into a list of file paths and deletes them.
    """

    for path in paths:
        logging.info('Deleting {0}'.format(path))
        if not DRY:
            remove(path)


def upload_files(*file_paths):
    """
    Uploads files to the specified Amazon S3 bucket.
    """

    for path in file_paths:
        file_name = basename(path)
        logging.info('Uploading {0} to Amazon S3'.format(path))
        if not DRY:
            with open(path, 'rb') as f:
                S3.put_object(
                    Bucket = config['aws-bucket'],
                    Key = file_name,
                    Body = f,
                    ACL = 'public-read',
                )


def autolink_posts(text):
    """
    Searches a string of text for substrings that look like posts (/XXX) and
    replaces them with <a> tags to the specified post.
    """

    if not text: return ''
    return re.sub(
        r'(^|\W)/(\d+)',
        '\g<1><a href="http://{}/\g<2>">/\g<2></a>'.format(config['domain']),
        text,
    )


def resize_image(img, metadata):
    """
    Resizes an image into four different sizes.

    Parameters
    ----------
    img: A `PIL.Image` to be resized.
    metadata: A dictionary of EXIF data for the image. Used to determine the
    image's orientation, because it might need to be rotated.

    Returns
    -------
    A list of four resized `PIL.Image`s.
    """

    degree_to_rotate = ORIENTATIONS[metadata.get('Orientation', 0)]
    if degree_to_rotate is not None:
        img = img.rotate(degree_to_rotate, expand = True)

    width, height = img.size
    larger_dimension = width if width > height else height
    scales = [ x / larger_dimension for x in [ 320.0, 640.0, 960.0, 1280.0 ] ]
    new_sizes = [ (round(width * s), round(height * s)) for s in scales ]
    return [ img.resize(size, Image.LANCZOS) for size in new_sizes ]


def create_img_tag(oid, widths, summary):
    """
    Creates an HTML <img> tag for an image post. Uses the OID, widths, and
    optional summary for the different components of the tag.

    Parameters
    ----------
    oid: A number representing the OID of the <img>'s associated post.
    widths: A list of numbers representing each width of the image.
    summary: A summary image that, if truthy, will cause an "alt" attribute to
    be added to the tag.

    Returns
    -------
    A string <img> tag.
    """

    assets_url = '{{ site.assets_url }}'

    # Use the second-to-smallest file (widths[1]) as the default.
    src = '%s/%d-%d.jpg' % (assets_url, oid, widths[1])
    srcset = [ '%s/%d-%d.jpg %dw' % (assets_url, oid, w, w) for w in widths ]
    img_tag = '<img src="{0}" '.format(src)
    img_tag += 'srcset="{0}, {1}, {2}, {3}" '.format(*srcset)
    img_tag += 'sizes="(min-width: 700px) 50vw, calc(100vw - 2rem)" '
    img_tag += 'alt="{{ page.summary }}" ' if summary else ''
    img_tag += '/>'

    return img_tag

def process_image(post_object, img_path):
    """
    Processes an uploaded image file, extract information from it to generate
    a post.

    Parameters
    ----------
    post_object: A dictionary of post data that will be updated.
    img_path: A temp path to the uploaded image file.
    """

    oid = post_object['oid']

    logging.info('Making image post #%s' % oid)

    img = Image.open(img_path)

    metadata = get_img_data(img)

    # Attempt to extract the date the image was captured from the metadata.
    if 'DateTime' in metadata:
        dt = metadata['DateTime']
        post_object['date'] = dt.split(' ')[0].replace(':', '-')

    logging.info('Resizing image #%s (%s)' % (oid, img_path))

    # 1. Get list of resized `Image`s.
    resized = resize_image(img, metadata)

    # 2. Make a list of their widths.
    widths = [ r.size[0] for r in resized ]

    # 3. Save them as {oid}-{width}.jpg in a temporary location.
    new_files = [ join(TEMP_PATH, '%d-%d.jpg' % (oid, w)) for w in widths ]
    for r, f in zip(resized, new_files):
        r.save(f, optimize = True, progressive = True)
        r.close()

    img.close()

    # Upload resized images to S3.
    upload_files(*new_files)

    # Clean up temporary files.
    delete(img_path, *new_files)

    # Use the largest of the resized images for the OpenGraph image meta tag.
    post_object['og_image'] = '%d-%d.jpg' % (oid, max(widths))
    post_object['content'] = create_img_tag(oid, widths, post_object['summary'])


def create_post(post_object):
    """
    Converts a post object dictionary into an actual post and writes it
    to a file.

    Parameters
    ----------
    post_object: A dictionary of data for the post. Includes things like OID,
    content, summary, date, etc.
    """

    oid = post_object['oid']

    logging.info('Writing post #{0}'.format(oid))

    today = datetime.date.today()

    if 'date' in post_object:
        date = datetime.datetime.strptime(post_object['date'], '%Y-%m-%d')
    else:
        date = today

    date_str = '{d:%B} {d.day}, {d:%Y}'.format(d = date)

    lines = [
        '---',
        'layout: post',
        "summary: '%s'" % (post_object['summary'] or 'Post #%d' % oid)
    ]

    if 'og_image' in post_object:
        lines.append('og_image: %s' % post_object['og_image'])

    lines.extend([
        '---',
        '',
        '<p>',
        '  <time>',
        '    <a href="/%s">%s</a>' % (oid, date_str),
        '  </time>',
        '  <a href="/%s">' % oid,
        '    %s' % post_object['content'],
        '  </a>',
    ])

    summary = post_object['summary']
    if summary:
        lines.append('  <span>%s</span>' % autolink_posts(summary))

    lines.extend([ '</p>', '' ])
    contents = '\n'.join(lines)

    logging.debug(contents)

    file_name = rel('blog/_posts/{0}-{1}.md'.format(str(today), oid))
    if not DRY:
        with open(file_name, 'w') as f:
            f.write(contents)

def update_site(new_post_number):
    """
    Adds a new post and pushes the site to GitHub, where it will be republished.

    Parameters
    ----------
    new_post_number: The OID/number of the new post (used for logging and for
    generating the commit message.)
    """

    logging.info('Uploading blog post #{0}'.format(new_post_number))

    if not DRY:
        with pushd(uploader_dirpath):
            git.add('_posts')
            git.commit('-m', 'Add post {0}'.format(new_post_number))
            git.push('origin', 'master')


@post('/upload')
def upload():

    if not is_authorized():
        logging.error('Unauthorized request to /upload')
        abort(406)

    # Verify that the request is legitimate.
    timestamp = request.forms.get('timestamp')
    token = request.forms.get('token')
    signature = request.forms.get('signature')
    verify_mailgun_request(timestamp, token, signature)

    # Ensure the local blog copy is up to date.
    with pushd(uploader_dirpath):
        git.pull('origin', 'master')

    try:

        post_object = {}

        new_oid = get_new_oid()
        post_object['oid'] = new_oid

        summary = request.forms.get('subject', '')
        post_object['summary'] = html.escape(summary)

        fpath, ftype = download_attachments(request.forms.get('attachments'))

        # This section creates the main content for the post, based on the
        # type of uploaded file. The `process_<type>` functions update the
        # `post_object` with values that will be used to write the post,
        # but also perform side effects (like resizing, uploading, etc.)
        if ftype.startswith('image'):
            process_image(post_object, fpath)

        create_post(post_object)

        update_site(new_oid)

    except Exception as e:
        logging.exception(e)
        abort(406)


if __name__ == '__main__':
    logging.info('Starting server')
    run(host = 'localhost', port = 8080)
