import datetime
import json
import os
from unittest.mock import patch, mock_open, Mock, call, DEFAULT

from nose.tools import eq_, raises, assert_raises
from PIL import Image

old_mode = os.environ.get('MODE', None)
os.environ['MODE'] = 'test'

from server import (
    verify_mailgun_request,
    download_attachments,
    get_new_oid,
    get_img_data,
    delete,
    upload_files,
    autolink_posts,
    resize_image,
    create_img_tag,
    process_image,
    create_post,
)

def teardown():
    if old_mode:
        os.environ['MODE'] = old_mode
    else:
        del os.environ['MODE']

@patch('time.time', Mock(return_value = 1501718220))
def test_verify_mailgun_request_successful():

    # If the function completes without raising, it was successful.
    verify_mailgun_request(
        '1501718219',
        'beefc0ffeef00dc001',
        '6f18a769135bdb32fec4a4d24ee7efff082f2d23646f25ea790b883d1a7805ea',
    )

@patch('time.time', Mock(return_value = 1501718220))
def test_verify_mailgun_request_duplicate_token():

    verify_mailgun_request(
        '1501718219',
        'c0ffeec001f00dbeef',
        'e473b85d8eaa5c71f2574dd81f42b7b478d1a320515d337ca8c7022419aacb84',
    )

    # Token is identical to the token in the previous request.
    with assert_raises(ValueError) as err:

        verify_mailgun_request(
            '1501718219',
            'c0ffeec001f00dbeef',
            'e473b85d8eaa5c71f2574dd81f42b7b478d1a320515d337ca8c7022419aacb84',
        )

        expected_msg = 'Mailgun token is identical to the previous one'
        assert str(err.exception) == expected_msg

def test_verify_mailgun_request_expired_timestamp():

    # This timestamp is older than 60s.
    with assert_raises(ValueError) as err:

        verify_mailgun_request(
            '1501718219',
            'aceaceaceaceaceaceaceace',
            'e787eb21731cf1888e078796224c23c3327497668e0a83a23bcda76b19b23c20',
        )

        expected_msg = 'Mailgun timestamp is older than 60 seconds'
        assert str(err.exception) == expected_msg

@patch('time.time', Mock(return_value = 1501718220))
def test_verify_mailgun_request_invalid_signature():

    with assert_raises(ValueError) as err:

        verify_mailgun_request(
            '1501718219',
            'c001c001c001',
            'this-test-is-not-gonna-work-because-the-signature-is-not-invalid',
        )

        expected_msg = 'Computed signature does not match request signature'
        assert str(err.exception) == expected_msg

@patch('requests.get')
@patch('server.open', mock_open(), create = True)
def test_successful_download_attachments(_):

    attachments = json.dumps([{
        'url': 'http://download.attachment/successful-image.jpg',
        'name': 'successful-image.jpg',
        'content-type': 'image/jpeg',
    }])

    save_path, content_type = download_attachments(attachments)

    eq_(save_path, '/tmp/successful-image.jpg')
    eq_(content_type, 'image/jpeg')

@raises(IndexError)
def test_download_attachments_no_attachment():
    attachments = json.dumps([])
    download_attachments(attachments)

def test_download_attachments_wront_type():
    attachments = json.dumps([{
        'url': 'http://download.attachment/bad-type.txt',
        'name': 'bad-type.txt',
        'content-type': 'text/plain',
    }])

    with assert_raises(ValueError) as err:
        download_attachments(attachments)
        assert str(err.exception) == "Unsupported file type 'text/plain'"

@patch('server.listdir', Mock(return_value = [
    '2012-05-15-0.md',
    '2016-11-02-1.md',
    '2017-01-16-3.md',
]))
def test_get_new_oid():
    eq_(get_new_oid(), 4)

def test_get_img_data():
    img = Mock()
    img._getexif = Mock(return_value = { 274: 3, 306: '2015:04:02' })
    exif = get_img_data(img)
    eq_(exif, { 'Orientation': 3, 'DateTime': '2015:04:02' })

def test_get_img_data_no_data():
    img = Mock()
    img._getexif = Mock(side_effect = AttributeError)
    exif = get_img_data(img)
    eq_(exif, {})

@patch('server.remove')
def test_delete(remove):
    delete('1', '2', '3')
    eq_(remove.call_args_list, [ call('1'), call('2'), call('3') ])

@raises(OSError)
@patch('server.remove', Mock(side_effect = OSError))
def test_delete_error():
    delete('os error')

@patch('server.open', mock_open(), create = True)
@patch('botocore.client.BaseClient._make_api_call')
def test_upload_files(put_object):
    files = [ '/tmp/a.jpg', '/tmp/b.jpg', '/tmp/c.jpg' ]
    upload_files(*files)
    for i, f in enumerate(files):
        op, args = put_object.call_args_list[i][0]
        eq_(op, 'PutObject')

        # Ignore the 'Body' arg, which is a `MagicMock` object.
        del args['Body']

        eq_(args, {
            'Bucket': 'aws.bucket',
            'Key': os.path.basename(f),
            'ACL': 'public-read',
        })

def test_autolink_posts():

    specs = {
        'Pic of Joe, see /644': 'Pic of Joe, see <a href="http://foo.bar/644">/644</a>',
        'Cool pic, <3 //6': 'Cool pic, <3 /<a href="http://foo.bar/6">/6</a>',
        'A pic, /s': 'A pic, /s',
        'This pic is 10/10!': 'This pic is 10/10!',
        '/164 is similar': '<a href="http://foo.bar/164">/164</a> is similar',
        '(/322,/333)': '(<a href="http://foo.bar/322">/322</a>,<a href="http://foo.bar/333">/333</a>)',
    }

    for summary, expected in specs.items():
        yield eq_, autolink_posts(summary), expected

def test_resize_image():

    img = Image.new('RGBA', size = (1600, 1200))
    resized = resize_image(img, {})
    assert len(resized) == 4
    assert resized[0].size == (320, 240)
    assert resized[1].size == (640, 480)
    assert resized[2].size == (960, 720)
    assert resized[3].size == (1280, 960)

    resized = resize_image(img, { 'Orientation': 6 })
    assert len(resized) == 4
    assert resized[0].size == (240, 320)
    assert resized[1].size == (480, 640)
    assert resized[2].size == (720, 960)
    assert resized[3].size == (960, 1280)

def test_create_image_tag():

    SPECS = [
        (
            ( 777, [ 300, 500, 700, 900 ], '' ),
            '<img src="{{ site.assets_url }}/777-500.jpg" srcset="{{ site.assets_url }}/777-300.jpg 300w, {{ site.assets_url }}/777-500.jpg 500w, {{ site.assets_url }}/777-700.jpg 700w, {{ site.assets_url }}/777-900.jpg 900w" sizes="(min-width: 700px) 50vw, calc(100vw - 2rem)" />',
        ),
        (
            ( 888, [ 200, 400, 600, 800 ], 'Summary' ),
            '<img src="{{ site.assets_url }}/888-400.jpg" srcset="{{ site.assets_url }}/888-200.jpg 200w, {{ site.assets_url }}/888-400.jpg 400w, {{ site.assets_url }}/888-600.jpg 600w, {{ site.assets_url }}/888-800.jpg 800w" sizes="(min-width: 700px) 50vw, calc(100vw - 2rem)" alt="{{ page.summary }}" />',
        ),
    ]

    for args, expected in SPECS:
        yield eq_, create_img_tag(*args), expected

@patch('PIL.Image.open')
@patch.multiple(
    'server',
    create_img_tag = DEFAULT,
    upload_files = DEFAULT,
    delete = DEFAULT,
    resize_image = DEFAULT,
    get_img_data = DEFAULT,
)
def test_process_image(
    Image_open,
    get_img_data,
    resize_image,
    delete,
    upload_files,
    create_img_tag,
):

    # Setup

    get_img_data.return_value = { 'DateTime': '2017:05:05 13:21:05' }

    resized = [
        Mock(size = (150, 100)),
        Mock(size = (200, 300)),
        Mock(size = (300, 450)),
        Mock(size = (500, 750)),
    ]
    resize_image.return_value = resized

    create_img_tag.return_value = '<img src="111.jpg" />'

    # Call

    post_object = { 'oid': 111, 'summary': 'Hi hello' }
    process_image(post_object, '/path/to/file.jpg')

    # Assert

    Image_open.assert_called_once_with('/path/to/file.jpg')

    resized[0].save.assert_called_once_with('/tmp/111-150.jpg')
    resized[1].save.assert_called_once_with('/tmp/111-200.jpg')
    resized[2].save.assert_called_once_with('/tmp/111-300.jpg')
    resized[3].save.assert_called_once_with('/tmp/111-500.jpg')

    upload_files.assert_called_once_with(
        '/tmp/111-150.jpg',
        '/tmp/111-200.jpg',
        '/tmp/111-300.jpg',
        '/tmp/111-500.jpg',
    )

    delete.assert_called_once_with(
        '/path/to/file.jpg',
        '/tmp/111-150.jpg',
        '/tmp/111-200.jpg',
        '/tmp/111-300.jpg',
        '/tmp/111-500.jpg',
    )

    assert post_object == {
        'oid': 111,
        'summary': 'Hi hello',
        'date': '2017-05-05',
        'og_image': '111-500.jpg',
        'content': '<img src="111.jpg" />',
    }

def test_create_post():

    today = datetime.datetime.today()
    today_str = today.isoformat().split('T')[0]

    SPECS = [
        (
            {
                'oid': 872,
                'date': '1992-11-16',
                'summary': 'Apples &amp; Bananas',
                'og_image': '872-1280.jpg',
                'content': '<img src="872-1280.jpg" />',
            },
            '\n'.join([
                '---',
                'layout: post',
                "summary: 'Apples &amp; Bananas'",
                'og_image: 872-1280.jpg',
                '---',
                '',
                '<p>',
                '  <time>',
                '    <a href="/872">November 16, 1992</a>',
                '  </time>',
                '  <a href="/872">',
                '    <img src="872-1280.jpg" />',
                '  </a>',
                '  <span>Apples &amp; Bananas</span>',
                '</p>',
                '',
            ])
        ),
        (
            {
                'oid': 431,
                'date': '2004-01-31',
                'summary': '',
                'content': '<img src="431-960.jpg" />',
            },
            '\n'.join([
                '---',
                'layout: post',
                "summary: 'Post #431'",
                '---',
                '',
                '<p>',
                '  <time>',
                '    <a href="/431">January 31, 2004</a>',
                '  </time>',
                '  <a href="/431">',
                '    <img src="431-960.jpg" />',
                '  </a>',
                '</p>',
                '',
            ])
        ),
    ]

    def make_assertion(post_object, expected):
        with patch('server.open', mock_open(), create = True) as m:
            oid = post_object['oid']
            post_path = 'blog/_posts/%s-%d.md' % ( today_str, oid )
            post_path = os.path.join(os.getcwd(), post_path)
            create_post(post_object)
            m.assert_called_once_with(post_path, 'w')
            handle = m()
            handle.write.assert_called_once_with(expected)

    for post_object, expected in SPECS:
        yield make_assertion, post_object, expected
