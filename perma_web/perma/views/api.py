from datetime import datetime, timedelta
import logging, json, subprocess, urllib2, re, os
from datetime import datetime
from urlparse import urlparse

import lxml.html, requests
from PIL import Image
from pyPdf import PdfFileReader

from perma.models import Link, Asset
from perma.forms import UploadFileForm
from perma.utils import base
from perma.tasks import get_screen_cap, get_source, store_text_cap, get_pdf

from django.shortcuts import render_to_response, HttpResponse
from django.http import HttpResponseBadRequest
from django.conf import settings
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


def linky_post(request):
    """
    We've received a request to archive a URL. That process is managed here.
    We create a new entry in our datastore and pass the work off to our indexing
    workers. They do their thing, updating the model as they go. When we get some minimum
    set of results we can present the user (a title and an image capture of the page), we respond
    back.
    """

    # Sometimes a tab or a space gets placed in the form field (by the
    # user copying and pasting?). Trim it here.
    target_url = request.POST.get('url').strip(' \t\n\r').replace(" ", "")

    # If we don't get a protocol, assume http
    if target_url[0:4] != 'http':
        target_url = 'http://' + target_url

    # Does this thing look like a valid URL?
    validate = URLValidator()
    try:
        validate(target_url)
    except ValidationError, e:
        return HttpResponse(status=400)

    # Somtimes we can't get a title from the markup. If not, use the domain
    url_details = urlparse(target_url)
    target_title = url_details.netloc

    # Get the markup. We get the mime-type and the title from this.
    try:
        r = requests.get(target_url)
        parsed_html = lxml.html.fromstring(r.content)
    except IOError:
        logger.debug("Title capture from markup failed for %s, using the hostname" % target_url)

    if len(parsed_html):
        if parsed_html.find(".//title") is not None and parsed_html.find(".//title").text:
            target_title = parsed_html.find(".//title").text.strip()

    # We have some markup and a title. Let's create a linky from it
    link = Link(submitted_url=target_url, submitted_title=target_title)

    if request.user.is_authenticated():
        link.created_by = request.user

    link.save()

    # Assets get stored in /storage/path/year/month/day/hour/unique-id/*
    # Get that path that we'll pass off to our workers to do the indexing. They'll store their results here
    now = dt = datetime.now()
    time_tuple = now.timetuple()

    path_elements = [str(time_tuple.tm_year), str(time_tuple.tm_mon), str(time_tuple.tm_mday), str(time_tuple.tm_hour), str(time_tuple.tm_min), link.guid]

    # Create a stub for our assets
    asset, created = Asset.objects.get_or_create(link=link)
    asset.base_storage_path = os.path.sep.join(path_elements)
    asset.save()

    # If it appears as if we're trying to archive a PDF, only run our PDF retrieval tool
    if r.headers['content-type'] in ['application/pdf', 'application/x-pdf'] or target_url.split('.')[-1] == 'pdf':
        get_pdf.delay(link.guid, target_url, os.path.sep.join(path_elements), request.META['HTTP_USER_AGENT'])
        response_object = {'linky_id': link.guid, 'message_pdf': True, 'linky_title': link.submitted_title}
        
    else: # else, it's not a PDF. Let's try our best to retrieve what we can
    
        # We pass the guid to our tasks
        guid = link.guid

        asset.image_capture = 'pending'        
        asset.text_capture = 'pending'
        asset.warc_capture = 'pending'
        asset.save()
        
        # Run our synchronus screen cap task (use the headless browser to create a static image)
        get_screen_cap(guid, target_url, os.path.sep.join(path_elements), request.META['HTTP_USER_AGENT'])

        # Get the text capture of the page (through a service that follows pagination)
        store_text_cap.delay(target_url, target_title, guid)
        
        # Try to crawl the page (but don't follow any links)
        get_source.delay(guid, target_url, os.path.sep.join(path_elements), request.META['HTTP_USER_AGENT'])
        
        asset = Asset.objects.get(link__guid=guid)
        
        response_object = {'linky_id': link.guid, 'linky_title': link.submitted_title}
        
        # Sometimes our phantomjs capture fails. if it doesn't add it to our response object
        if asset.image_capture != 'pending' and asset.image_capture != 'failed':
            response_object['linky_cap'] = settings.STATIC_URL + asset.base_storage_path + '/' + asset.image_capture

    return HttpResponse(json.dumps(response_object), content_type="application/json", status=201)


def validate_upload_file(upload):
    # Make sure files are not corrupted.
    extention = upload.name[-4:]
    if extention in ['.jpg', 'jpeg']:
        try:
            i = Image.open(upload)
            if i.format == 'JPEG':
                return True
        except IOError:
            return False
    elif extention == '.png':
        try:
            i = Image.open(upload)
            if i.format =='PNG':
                return True
        except IOError:
            return False
    elif extention == '.pdf':
        doc = PdfFileReader(upload)
        if all([doc.documentInfo, doc.numPages]):
            return True
    return False


def upload_file(request):
    if request.method == 'POST':
        form = UploadFileForm(request.POST, request.FILES)
        if form.is_valid():
            if validate_upload_file(request.FILES['file']) and request.FILES['file'].size <= settings.MAX_ARCHIVE_FILE_SIZE:
                link = Link(submitted_url=form.cleaned_data['url'], submitted_title=form.cleaned_data['title'])
                
                if request.user.is_authenticated():
                  link.created_by = request.user
                link.save()
                
                now = dt = datetime.now()
                time_tuple = now.timetuple()
                path_elements = [str(time_tuple.tm_year), str(time_tuple.tm_mon), str(time_tuple.tm_mday), str(time_tuple.tm_hour), str(time_tuple.tm_min), link.guid]

                linky_home_disk_path = settings.GENERATED_ASSETS_STORAGE + '/' + os.path.sep.join(path_elements)

                if not os.path.exists(linky_home_disk_path):
                    os.makedirs(linky_home_disk_path)

                asset, created = Asset.objects.get_or_create(link=link)
                asset.base_storage_path = os.path.sep.join(path_elements)
                asset.save()

                file_name = '/cap.' + request.FILES['file'].name.split('.')[-1]

                if request.FILES['file'].name.split('.')[-1] == 'pdf':
                    asset.pdf_capture = file_name
                else:
                    asset.image_capture = file_name

                    #print linky_home_disk_path + file_name
                    #png = PythonMagick.Image(linky_home_disk_path + file_name)
                    #png.write("file_out.png")
                    #params = ['convert', linky_home_disk_path + file_name, 'out.png']
                    #subprocess.check_call(params)

                asset.save()
                
                request.FILES['file'].file.seek(0)
                f = open(linky_home_disk_path + file_name, 'w')
                f.write(request.FILES['file'].file.read())
                os.fsync(f)
                f.close()

                response_object = {'status':'success', 'linky_id':link.guid, 'linky_hash':link.guid}

                """try:
                    get_source.delay(link.guid, target_url, os.path.sep.join(path_elements), request.META['HTTP_USER_AGENT'])
                    store_text_cap.delay(target_url, target_title, asset)
                except Exception, e:
                    # TODO: Log the failed url
                    asset.pdf_capture = 'failed'
                    asset.save()"""

                return HttpResponse(json.dumps(response_object), 'application/json')
            else:
                return HttpResponseBadRequest(json.dumps({'status':'failed', 'reason':'Invalid file.'}), 'application/json')
        else:
            return HttpResponseBadRequest(json.dumps({'status':'failed', 'reason':'Missing file.'}), 'application/json')
            
    #return HttpResponseBadRequest(json.dumps({'status':'failed', 'reason':form.errors}), 'application/json')


def urldump(request, since=None):
    """
    Give basic JSON encoding of GUID/URL pairs created since 'since'
    """
    ### XXX some day we should probably cache this and/or use some
    ### sort of sweet checkpointing system, but this is a start
    if since is None:
        dt = datetime.now() - timedelta(days=1)
    else:
        dt = datetime.strptime(since, "%Y-%m-%d")
    links = Link.objects.filter(creation_timestamp__gte=dt)
    data = []
    for link in links:
        datum = {'guid': link.guid, 'url': link.submitted_url}
        data.append(datum)
    response = json.dumps(data)
    return HttpResponse(response, 'application/json')
