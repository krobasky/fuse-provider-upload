import datetime
import os
import shutil
import uuid
from multiprocessing import Process
from typing import List

import aiofiles
import docker
import pymongo

from docker.errors import ContainerError
from fastapi import FastAPI, Depends, Path, Query, Body, File, UploadFile
from fastapi.logger import logger
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse

from bson.json_util import dumps, loads

from fuse.models.Objects import Passports, ProviderExampleObject

#------------------
# xxx separate registry service
import pymongo
mongo_client = pymongo.MongoClient('mongodb://%s:%s@tx-persistence:27018/test' % (os.getenv('MONGO_NON_ROOT_USERNAME'), os.getenv('MONGO_NON_ROOT_PASSWORD')))
mongo_db = mongo_client["test"]
mongo_db_datasets_column = mongo_db["uploads"]

# xxx separate queue service
from redis import Redis
from rq import Queue, Worker
from rq.job import Job
# queue
redis_connection = Redis(host='redis', port=6379, db=0)
q = Queue(connection=redis_connection, is_async=True, default_timeout=3600)
def initWorker():
    worker = Worker(q, connection=redis_connection)
    worker.work()

#------------------


app = FastAPI()

origins = [
    f"http://{os.getenv('HOSTNAME')}:{os.getenv('HOSTPORT')}",
    f"http://{os.getenv('HOSTNAME')}",
    "http://localhost:{os.getenv('HOSTPORT')}",
    "http://localhost",
    "*",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import pathlib
import json

# xxx check File is an archive
@app.post("/submit", description="Submit a digital object to be stored by this data provider")
async def upload(submitter_id: str = Query(default=None, description="unique identifier for the submitter (e.g., email)"),
                 apikey: str = Query(default=None, description="optional API key for submitter to provide for using this or any third party apis required for submitting the object"),
                 requested_object_id: str = Query(default=None, description="optional argument to be used by submitter to request an object_id; this could be, for example, used to retrieve objects from a 3rd party for which this endpoint is a proxy. The requested object_id is not guaranteed, enduser should check return value for final object_id used."),
                 archive: UploadFile = File(...)):
    '''
    Parameters such as username/email for the submitter and parameter formats will be returned by the service-info endpoint to allow dynamic construction of dashboard elements
    '''
    # write data to memory
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    object_id = "upload_" + submitter_id + "_" + str(uuid.uuid4())

    # instantiate task
    # xxx throws error: TypeError: cannot serialize '_io.BufferedRandom' object
    # xxx without 'archive', error: redis.exceptions.ConnectionError: Error -2 connecting to redis:6379. Name or service not known.
    q.enqueue(run_upload, args=(object_id, submitter_id, archive), job_id=object_id, job_timeout=3600, result_ttl=-1)
    p_worker = Process(target=initWorker)
    p_worker.start()
    return {"object_id": object_id}

# xxx check param defaults
async def run_upload(object_id: str = None, submitter_id: str = None, archive: UploadFile=File(...)):
    local_path = os.getenv('HOST_ABSOLUTE_PATH')

    job = Job.fetch(object_id, connection=redis_connection)
    task_mapping_entry = {"object_id": object_id}
    new_values = {"$set": {"start_date": datetime.datetime.utcnow(), "status": job.get_status()}} # xxx status = started?
    mongo_db_datasets_column.update_one(task_mapping_entry, new_values)

    # xxx enqueue the following in the case of very large files
    # xxx break this out into registry service
    task_mapping_entry = {"task_id": task_id, "submitter_id": submitter_id, "status": None, "stderr": None, "date_created": datetime.datetime.utcnow(), "start_date": None, "end_date": None}
    mongo_db_datasets_column.insert_one(task_mapping_entry)

    
    local_path = os.path.join(local_path, f"{object_id}-data")
    os.mkdir(local_path)

    # xxxget filename? Use queue?
    file_path = os.path.join(local_path, "upload.gz")
    async with aiofiles.open(file_path, 'wb') as out_file:
        content = await archive.read()
        await out_file.write(content)

    new_values = {"$set": {"start_date": datetime.datetime.utcnow(), "status": "completed"}}
    mongo_db_datasets_column.update_one(task_mapping_entry, new_values)
    #xxx catch errors
    return {"object_id": object_id}


@app.get("/objects/search/{submitter_id}", summary="Get infos for all the DrsObject for this submitter_id.")
async def objects_search(submitter_id: str = Path(default="", description="submitter_id of user that uploaded the archive")):
    query = {"submitter_id": submitter_id}
    ret = list(map(lambda a: a, mongo_db_datasets_column.find(query, {"_id": 0, "object_id": 1})))
    return ret

@app.get("/objects/status/{object_id}")
def upload_status(object_id: str):
    try:
        # xxx break this out into common queue service
        job = Job.fetch(object_id, connection=redis_connection)
        status = job.get_status()
        if (status == "failed"):
            # If job failed, add more detail
            # xxx break this out into common registry service
            upload_query = {"object_id": object_id}
            projection = {"_id": 0, "submitter_id": 1, "status": 1, "stderr": 1, "date_created": 1, "start_date": 1, "end_date": 1}
            entry = mongo_db_upload_column.find(upload_query, projection)
            ret =  {
                "status": status,
                "message":loads(dumps(entry.next()))
            }
        else:
            ret = {"status": status}

        mongo_db_datasets_column.update_one({"object_id": object_id}, {"$set": ret})
        return ret
    except Exception as e:
        raise HTTPException(status_code=404,
                            detail="! Exception {0} occurred while checking job status for ({1}), message=[{2}] \n! traceback=\n{3}\n".format(type(e), e, traceback.format_exc(), object_id))

@app.delete("/delete/{object_id}", summary="DANGER ZONE: Delete a downloaded object; this action is rarely justified.")
async def delete(object_id: str):
    '''
    Delete cached data from the remote provider, identified by the provided object_id.
    <br>**WARNING**: This will orphan associated analyses; only delete downloads if:
    - the data are redacted.
    - the system state needs to be reset, e.g., after testing.
    - the sytem state needs to be corrected, e.g., after a bugfix.

    <br>**Note**: If the object was changed on the data provider's server, the old copy should be versioned in order to keep an appropriate record of the input data for past dependent analyses.
    <br>**Note**: Object will be deleted from disk regardless of whether or not it was found in the database. This can be useful for manual correction of erroneous system states.
    <br>**Returns**: 
    - status = 'deleted' if object is found in the database and 1 object successfully deleted.
    - status = 'exception' if an exception is encountered while removing the object from the database or filesystem, regardless of whether or not the object was successfully deleted, see other returned fields for more information.
    - status = 'failed' if 0 or greater than 1 object is not found in database.
    '''
    delete_status = "done"

    # Delete may be requested while the download job is enqueued, so check that first:
    ret_job=""
    ret_job_err=""
    try:
        job = Job.fetch(immunespace_download_id, connection=redis_connection)
        if job == None:
            ret_job="No job found in queue. \n"
        else:
            job = job.delete(remove_from_queue=True)
    except Exception as e:
        # job is not expected to be on queue so don't change deleted_status from "done"
        ret_job_err += "! Exception {0} occurred while deleting job from queue: message=[{1}] \n! traceback=\n{2}\n".format(type(e), e, traceback.format_exc())
                        
        delete_status = "exception"

    # Assuming the job already executed, remove any database records
    ret_mongo=""
    ret_mongo_err=""
    try:
        task_query = {"immunespace_download_id": immunespace_download_id}
        ret = mongo_db_immunespace_downloads_column.delete_one(task_query)
        #<class 'pymongo.results.DeleteResult'>
        delete_status = "deleted"
        if ret.acknowledged != True:
            delete_status = "failed"
            ret_mongo += "ret.acknoledged not True.\n"
        if ret.deleted_count != 1:
            # should never happen if index was created for this field
            delete_status = "failed"
            ret_mongo += "Wrong number of records deleted ("+str(ret.deleted_count)+")./n"
        ## xxx
        # could check if there are any remaining; but this should instead be enforced by creating an index for this columnxs
        # could check ret.raw_result['n'] and ['ok'], but 'ok' seems to always be 1.0, and 'n' is the same as deleted_count
        ##
        ret_mongo += "Deleted count=("+str(ret.deleted_count)+"), Acknowledged=("+str(ret.acknowledged)+")./n"
    except Exception as e:
        ret_mongo_err += "! Exception {0} occurred while deleting job from database, message=[{1}] \n! traceback=\n{2}\n".format(type(e), e, traceback.format_exc())

        delete_status = "exception"
        
    # Data are cached on a mounted filesystem, unlink that too if it's there
    ret_os=""
    ret_os_err=""
    try:
        local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        local_path = os.path.join(local_path, immunespace_download_id + f"-immunespace-data")
        
        shutil.rmtree(local_path,ignore_errors=False)
    except Exception as e:
        ret_os_err += "! Exception {0} occurred while deleting job from filesystem, message=[{1}] \n! traceback=\n{2}\n".format(type(e), e, traceback.format_exc())

        delete_status = "exception"

    ret_message = ret_job + ret_mongo + ret_os
    ret_err_message = ret_job_err + ret_mongo_err + ret_os_err
    return {
        "status": delete_status,
        "info": ret_message,
        "stderr": ret_err_message,
    }



# ----------------- GA4GH endpoints ---------------------
@app.get("/service-info", summary="Retrieve information about this service")
async def service_info():
    '''
    Returns information about the DRS service

    Extends the v1.0.0 GA4GH Service Info specification as the standardized format for GA4GH web services to self-describe.

    According to the service-info type registry maintained by the Technical Alignment Sub Committee (TASC), a DRS service MUST have:
    - a type.group value of org.ga4gh
    - a type.artifact value of drs

    e.g.
    ```
    {
      "id": "com.example.drs",
      "description": "Serves data according to DRS specification",
      ...
      "type": {
        "group": "org.ga4gh",
        "artifact": "drs"
      }
    ...
    }
    ```
    See the Service Registry Appendix for more information on how to register a DRS service with a service registry.
    '''
    service_info_path = pathlib.Path(__file__).parent / "service_info.json"
    with open(service_info_path) as f:
        return json.load(f)

    
# READ-ONLY endpoints follow the GA4GH DRS API, modeled below
# https://editor.swagger.io/?url=https://ga4gh.github.io/data-repository-service-schemas/preview/release/drs-1.2.0/openapi.yaml
    
@app.get("/objects/{object_id}", summary="Get info about a DrsObject.")
async def objects(object_id: str = Path(default="", description="DrsObject identifier"),
                  expand: bool = Query(default=False, description="If false and the object_id refers to a bundle, then the ContentsObject array contains only those objects directly contained in the bundle. That is, if the bundle contains other bundles, those other bundles are not recursively included in the result. If true and the object_id refers to a bundle, then the entire set of objects in the bundle is expanded. That is, if the bundle contains aother bundles, then those other bundles are recursively expanded and included in the result. Recursion continues through the entire sub-tree of the bundle. If the object_id refers to a blob, then the query parameter is ignored.")):
    '''
    Returns object metadata, and a list of access methods that can be used to fetch object bytes.
    '''
    example_object = ProviderExampleObject()
    return example_object.dict()

# xxx add value for passport example that doesn't cause server error
# xxx figure out how to add the following description to 'passports':
# the encoded JWT GA4GH Passport that contains embedded Visas. The overall JWT is signed as are the individual Passport Visas
@app.post("/objects/{object_id}", summary="Get info about a DrsObject through POST'ing a Passport.")
async def post_objects(object_id: str = Path(default="", description="DrsObject identifier"),
                       expand: bool = Query(default=False, description="If false and the object_id refers to a bundle, then the ContentsObject array contains only those objects directly contained in the bundle. That is, if the bundle contains other bundles, those other bundles are not recursively included in the result. If true and the object_id refers to a bundle, then the entire set of objects in the bundle is expanded. That is, if the bundle contains aother bundles, then those other bundles are recursively expanded and included in the result. Recursion continues through the entire sub-tree of the bundle. If the object_id refers to a blob, then the query parameter is ignored."),
                       passports: Passports = Depends(Passports.as_form)):
    '''
    Returns object metadata, and a list of access methods that can be
    used to fetch object bytes. Method is a POST to accomodate a JWT
    GA4GH Passport sent in the formData in order to authorize access.
    '''
    example_object = ProviderExampleObject()
    return example_object.dict()

@app.get("/objects/{object_id}/access/{access_id}", summary="Get a URL for fetching bytes")
async def get_objects(object_id: str=Path(default="", description="DrsObject identifier"),
                      access_id: str=Path(default="", description="An access_id from the access_methods list of a DrsObject")):
    '''
    Returns a URL that can be used to fetch the bytes of a
    DrsObject. This method only needs to be called when using an
    AccessMethod that contains an access_id (e.g., for servers that
    use signed URLs for fetching object bytes).
    '''
    return {
        "url": "http://localhost/object.zip",
        "headers": "Authorization: None"
    }

# xxx figure out how to add the following description to 'passports':
# the encoded JWT GA4GH Passport that contains embedded Visas. The overall JWT is signed as are the individual Passport Visas.
@app.post("/objects/{object_id}/access/{access_id}", summary="Get a URL for fetching bytes through POST'ing a Passport")
async def post_objects(object_id: str=Path(default="", description="DrsObject identifier"),
                       access_id: str=Path(default="", description="An access_id from the access_methods list of a DrsObject"),
                       passports: Passports = Depends(Passports.as_form)):
    '''
    Returns a URL that can be used to fetch the bytes of a
    DrsObject. This method only needs to be called when using an
    AccessMethod that contains an access_id (e.g., for servers that
    use signed URLs for fetching object bytes). Method is a POST to
    accomodate a JWT GA4GH Passport sent in the formData in order to
    authorize access.

    '''
    return {
        "url": "http://localhost/object.zip",
        "headers": "Authorization: None"
    }

