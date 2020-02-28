from code_api.dp_exceptions import *
from code_api.datadel_s3 import S3Object
from code_api.dp_crypto import secure_password_hash

import sys
import os
from pathlib import Path

import json
import shutil
import datetime

import traceback
import concurrent

import couchdb

import boto3
from boto3.s3.transfer import TransferConfig


class DPUser():
    '''
    A Data Delivery Portal user.

    Args:
        username (str):   Delivery Portal username
        password (str):   Delivery Portal password

    Attributes:
        username (str): Delivery Portal username
        password (str): Delivery Portal password
        id (str):       User ID
        role (str):     Facility or researcher
    '''

    def __init__(self, username=None, password=None):
        self.username = username
        self.password = password
        self.id = None
        self.role = None


class DataDeliverer():
    '''
    Instanstiates the delivery by logging the user into the Delivery Portal,
    checking the users access to the specified project, and uploads/downloads
    the data to the S3 storage.

    Args:
        config (str):           Path to file with user creds and project info
        username (str):         User spec. username, None if config used
        password (str):         User spec. password, None if config used
        project_id (str):       User spec. project ID, None if config used
        project_owner (str):    User spec. project owner, None if config used
        pathfile (str):         Path to file containing file paths
        data (tuple):           All paths to be uploaded/downloaded

    Attributes:
        method (str):           Delivery method, put or get
        project_id (str):       Project ID to upload to/download from
        project_owner (str):    Owner of the current project
        data (list):            Paths to files/folders
        tempdir (list):         Paths to temporary DP folders
        user (DPUser):          Data Delivery Portal user
        s3 (S3Object):          S3 connection object

    Raises:
        DeliveryPortalException:    Required info not found or access denied
        OSError:                    Temporary directory failure

    '''

    def __init__(self, config=None, username=None, password=None,
                 project_id=None, project_owner=None,
                 pathfile=None, data=None):
        # If none of username, password and config options are set
        # raise exception and quit execution -- dp cannot be accessed
        if all(x is None for x in [username, password, config]):
            raise DeliveryPortalException("Delivery Portal login credentials "
                                          "not specified. Enter: "
                                          "\n--username/-u AND --password/-pw,"
                                          " or --config/-c\n --owner/-o\n"
                                          "For help: 'dp_api --help'.")
        else:
            # put or get
            self.method = sys._getframe().f_back.f_code.co_name
            self.user = DPUser(username=username, password=password)
            self.project_id = project_id
            self.project_owner = project_owner
            self.data = None
            self.s3 = S3Object()

            self.check_user_input(config=config)

            dp_access_granted = self.check_dp_access()
            if dp_access_granted and self.user.id is not None:
                proj_access_granted, self.s3.project = \
                    self.check_project_access()
                if proj_access_granted and self.s3.project is not None:
                    # If no data to upload, cancel
                    if not data and not pathfile:
                        raise DeliveryPortalException(
                            "No data to be uploaded. Specify individual "
                            "files/folders using the --data/-d option one or "
                            "more times, or the --pathfile/-f. "
                            "For help: 'dp_api --help'"
                        )
                    else:
                        self.data = self.data_to_deliver(data=data,
                                                         pathfile=pathfile)
                else:
                    raise DeliveryPortalException(
                        f"Access to project {self.project_id} "
                        "denied. Delivery cancelled."
                    )
            else:
                raise DeliveryPortalException("Delivery Portal access denied! "
                                              "Delivery cancelled.")

            if self.data is not None:
                dirs_created, self.tempdir = self.create_directories()
                if not dirs_created:
                    raise OSError("Temporary directory could not be created. "
                                  "Unable to continue delivery. Aborting. ")

                self.s3.get_info(self.project_id)

    def __enter__(self):
        '''Allows for implementation using "with" statement.
        Building.'''

        return self

    def __exit__(self, exc_type, exc_value, tb):
        '''Allows for implementation using "with" statement.
        Tear it down. Delete class.'''

        if exc_type is not None:
            traceback.print_exception(exc_type, exc_value, tb)
            return False  # uncomment to pass exception through

        return True

    def couch_connect(self):
        '''Connects to a couchdb interface. Currently hard-coded.

        Returns:
            couchdb.client.Server:  CouchDB server instance.
            '''

        try:
            couch = couchdb.Server('http://delport:delport@localhost:5984/')
        except CouchDBException as cdbe:
            sys.exit(f"Database login failed. {cdbe}")
        else:
            return couch

    def check_user_input(self, config):
        '''Checks that the correct options and credentials are entered.

        Args:
            config:     File containing the users DP username and password,
                        and the project relating to the upload/download.
                        Can be used instead of inputing the creds separately.

        Raises:
            OSError:                    Config file not found or opened
            DeliveryOptionException:    Required information not found
        '''

        if config is not None:              # If config file entered
            user_config = Path(config).resolve()
            if user_config.is_file():      # and exist
                try:
                    with user_config.open(mode='r') as cf:
                        credentials = json.load(cf)
                except OSError as ose:
                    sys.exit(f"Could not open path-file {config}: {ose}")

                # Check that all credentials are entered and quit if not
                for c in ['username', 'password', 'project']:
                    if c not in credentials:
                        raise DeliveryOptionException(
                            f"The config file does not contain: '{c}'."
                        )

                self.user.username = credentials['username']
                self.user.password = credentials['password']
                self.project_id = credentials['project']
                if 'owner' in credentials:
                    self.project_owner = credentials['owner']
            else:
                raise OSError(f"Config file {config} does not exist. "
                              "Cancelling delivery.")

        else:
            if self.user.username is None or self.user.password is None:
                raise DeliveryOptionException(
                    "Delivery Portal login credentials not specified. "
                    "Enter --username/-u AND --password/-pw, or --config/-c."
                    "For help: 'dp_api --help'."
                )
            else:
                if self.project_id is None:
                    raise DeliveryOptionException(
                        "Project not specified. Enter project ID using "
                        "--project option or add to config file using "
                        "--config/-c option."
                    )

                # If no owner is set then assuming current user is owner
                if self.project_owner is None:
                    self.project_owner = self.user.username

        if self.project_owner is None and self.method == 'put':
            raise DeliveryOptionException("Project owner not specified. "
                                          "Cancelling delivery.")

    def check_dp_access(self):
        '''Checks the users access to the delivery portal

        Returns:
            tuple:  Granted access and user ID

                bool:   True if user login successful
                str:    User ID

        Raises:
            CouchDBException:           Database connection failure or
                                        user not found
            DeliveryPortalException:    Wrong password
        '''

        try:
            user_db = self.couch_connect()['user_db']
        except CouchDBException as cdbe:
            sys.exit(f"Could not collect database 'user_db'. {cdbe}")
        else:
            # Search the database for the user
            for id_ in user_db:
                # If found, create secure password hash
                if self.user.username == user_db[id_]['username']:
                    password_settings = user_db[id_]['password']['settings']
                    password_hash = \
                        secure_password_hash(
                            password_settings=password_settings,
                            password_entered=self.user.password
                        )
                    # Compare to correct password
                    if user_db[id_]['password']['hash'] != password_hash:
                        raise DeliveryPortalException(
                            "Wrong password. Access to Delivery Portal "
                            "denied."
                        )
                    else:
                        # Check that facility putting or researcher getting
                        self.user.role = user_db[id_]['role']
                        if (self.user.role == 'facility'
                            and self.method == 'put') \
                           or (self.user.role == 'researcher'
                                and self.method == 'get'):
                            self.user.id = id_
                            if (self.user.role == 'researcher'
                                and self.method == 'get'
                                    and self.project_owner is None):
                                self.project_owner = self.user.id

                            return True

            raise CouchDBException("Username not found in database. "
                                   "Access to Delivery Portal denied.")

    def check_project_access(self):
        '''Checks the users access to a specific project.

        Returns:
            tuple:  Project access and S3 project ID

                bool:   True if project access granted
                str:    S3 project to upload to/download from

        Raises:
            CouchDBException:           Database connection failure
                                        or missing project information
            DeliveryPortalException:    Access denied
            DeliveryOptionException:    S3 delivery option not available
                                        or incorrect project owner
        '''

        try:
            couch = self.couch_connect()  # Connect to database
        except CouchDBException as cdbe:
            sys.exit(f"Could not connect to CouchDB: {cdbe}")
        else:
            user_db = couch['user_db']
            # Get the projects registered to the user
            user_projects = user_db[self.user.id]['projects']
            # Check if project doesn't exists in project database -> quit
            if self.project_id not in couch['project_db']:
                raise CouchDBException(
                    f"The project {self.project_id} does not exist.")

            # If project exists, check if user has access to the project->quit
            if self.project_id not in user_projects:
                raise DeliveryPortalException(
                    "You do not have access to the specified project "
                    f"{self.project_id}. Aborting delivery."
                )

            # If user has access, get current project
            current_project = couch['project_db'][self.project_id]
            # If project information doesn't exist -> quit
            if 'project_info' not in current_project:
                raise CouchDBException(
                    "There is no 'project_info' recorded for the specified "
                    "project. Aborting delivery."
                )

            # If project info exists, check if owner info exists.
            # If not -> quit
            if 'owner' not in current_project['project_info']:
                raise CouchDBException("An owner of the data has not been "
                                       "specified. Cannot guarantee data "
                                       "security. Cancelling delivery.")

            # If owner info exists, find owner of project
            # and check if specified owner matches. If not -> quit
            correct_owner = current_project['project_info']['owner']
            # If facility specified correct user or researcher is owner
            if (self.method == 'put'
                and correct_owner == self.project_owner != self.user.id) \
                or (self.method == 'get'
                    and correct_owner == self.project_owner == self.user.id):
                # If delivery_option not recorded in database -> quit
                if 'delivery_option' not in current_project['project_info']:
                    raise CouchDBException("A delivery option has not been "
                                           "specified for this project.")

                # If delivery option exists, check if S3. If not -> quit
                if current_project['project_info']['delivery_option'] != "S3":
                    raise DeliveryOptionException(
                        "The specified project does not "
                        "have access to S3 delivery."
                    )

                # If S3 option specified, return S3 project ID
                try:
                    s3_project = user_db[self.user.id]['s3_project']['name']
                except DeliveryPortalException as dpe:
                    sys.exit("Could not get Safespring S3 project name from "
                             f"database: {dpe}. \nDelivery aborted.")
                else:
                    return True, s3_project
            else:
                raise DeliveryOptionException(
                    "Incorrect data owner! You do not have access to this "
                    "project. Cancelling delivery."
                )

    def data_to_deliver(self, data: tuple, pathfile: str) -> (list):
        '''Puts all entered paths into one list

        Args:
            data:       Tuple containing paths
            pathfile:   Path to file containing paths

        Returns:
            list:   List of all paths entered in data and pathfile option

        Raises:
            IOError:                    Pathfile not found
            DeliveryOptionException:    Multiple identical files or 
                                        false delivery method 
        '''

        all_files = list()

        # If --data option --> put all files in list
        if data is not None:
            if self.method == "put":
                all_files = [Path(d).resolve() if Path(d).exists()
                             else [None, d] for d in data]
            elif self.method == "get":
                all_files = [d for d in data]
            else:
                pass    # raise an error here

        # If --pathfile option --> put all files in list
        if pathfile is not None:
            pathfile_abs = Path(pathfile).resolve()
            # Precaution, already checked in click.option
            if pathfile_abs.exists():
                # Read lines, strip \n and put in list
                with pathfile_abs.open(mode='r') as file:
                    if self.method == "put":
                        all_files += \
                            [Path(line.strip()).resolve()
                             if Path(line.strip()).exists()
                             else [None, line.strip()] for line in file]
                    elif self.method == "get":
                        all_files += [line.strip() for line in file]
                    else:
                        raise DeliveryOptionException(
                            "Delivery option {self.method} not allowed. "
                            "Cancelling delivery.")
            else:
                raise IOError(f"- -pathfile option {pathfile} does not exist. "
                              "Cancelling delivery.")

        # Check for file duplicates
        for element in all_files:
            if all_files.count(element) != 1:
                raise DeliveryOptionException(
                    f"The path to file {element} is listed multiple times, "
                    "please remove path dublicates."
                )

        return all_files

    def create_directories(self):
        '''Creates all temporary directories.

        Returns:
            tuple:  Directories created and all paths

                bool:   True if directories created
                tuple:  All created directories

        Raises:
            IOError:    Temporary folder failure
        '''

        # Create temporary folder with timestamp and all subfolders
        timestamp_ = self.timestamp()
        temp_dir = Path.cwd() / Path(f"DataDelivery_{timestamp_}")
        dirs = tuple(temp_dir / Path(sf)
                     for sf in ["", "files/", "keys/", "meta/", "logs/"])
        for d_ in dirs:
            try:
                d_.mkdir()
            except IOError as ose:
                print(f"The directory '{d_}' could not be created: {ose}"
                      "Cancelling delivery. Deleting temporary directory.")

                if temp_dir.exists():
                    try:
                        # Remove all prev created folders
                        shutil.rmtree(temp_dir)
                        sys.exit(f"Temporary directory deleted. \n\n"
                                 "----DELIVERY CANCELLED---\n")  # and quit
                    except IOError as ose:
                        sys.exit(f"Could not delete directory {temp_dir}: "
                                 f"{ose}\n\n ----DELIVERY CANCELLED---\n")

                        return False, ()

                else:
                    pass  # create log file here

        return True, dirs

    def timestamp(self) -> (str):
        '''Gets the current time. Formats timestamp.

        Returns:
            str:    Timestamp in format 'YY-MM-DD_HH-MM-SS'

        '''

        now = datetime.datetime.now()
        timestamp = ""
        sep = ""

        for t in (now.year, "-", now.month, "-", now.day, " ",
                  now.hour, ":", now.minute, ":", now.second):
            if len(str(t)) == 1 and isinstance(t, int):
                timestamp += f"0{t}"
            else:
                timestamp += f"{t}"

        return timestamp.replace(" ", "_").replace(":", "-")

    def put(self, file: str, spec_path: str) -> (str):
        '''Uploads specified data to the S3 bucket.

        Args: 
            file:       File to be uploaded
            spec_path:  Root folder path to file
        '''

        filepath = ""           # Path to upload
        all_subfolders = ""     # All subfolders in a specific path 

        # Configs
        # MB = 1024 ** 2
        # GB = 1024 ** 3
        # config = TransferConfig(multipart_threshold=5*GB,
        #                         multipart_chunksize=5*MB)

        # check if bucket exists
        if self.s3.bucket in self.s3.resource.buckets.all():
            if spec_path is None:
                filepath = file.name   # file goes in root
            else:
                # New folder path for s3 storage from root 
                filepath = f"{spec_path}{str(file).split(spec_path)[-1]}"
                all_subfolders = f"{filepath.split(file.name)[0]}"

                # check if bucket exists 
                response = self.s3.resource.meta.client.list_objects_v2(
                    Bucket=self.s3.bucket.name,
                    Prefix="",
                )

                # Check if current folders exist in bucket 
                found = False
                for obj in response.get('Contents', []):
                    if obj['Key'] == all_subfolders:
                        found = True
                        break
                
                # Create folders if they don't exist 
                if not found:   # if folder doesn't exist then create folder
                    self.s3.resource.meta.client.put_object(
                        Bucket=self.s3.bucket.name,
                        Key=all_subfolders
                    )

            # Check if file exists in bucket folder path 
            file_already_in_bucket, filelist = \
                self.s3.file_exists_in_bucket(key=filepath)
            # Upload if doesn't exist 
            if file_already_in_bucket:
                return f"File exists: {file.name}, not uploading file."
            else:
                try:
                    # print("här")
                    # self.s3.bucket.upload_file(str(file), filepath)
                    self.s3.resource.meta.client.upload_file(
                        str(file), self.s3.bucket.name,
                        filepath, Config=TransferConfig(5*(1024**3)),
                        ExtraArgs={'ACL': 'bucket-owner-full-control'}, 
                        Callback=ProgressPercentage(str(file))
                    )
                except Exception as e:
                    print(f"{str(file)} not uploaded: ", e)
                else:
                    return f"Success: {file} uploaded to S3!"
        else:
            raise S3Error("The project does not have an S3 bucket."
                          "Unable to perform delivery.")

    def get(self, path: str) -> (str):
        '''Downloads specified data from S3 bucket

        Args: 
            file:           File to be downloaded
            dl_file:        Name of downloaded file

        Returns:
            str:    Success message if download successful 

        '''

        # Check if bucket exists 
        if self.s3.bucket in self.s3.resource.buckets.all():
            # Check if path exists in bucket
            file_in_bucket, filelist = self.s3.file_exists_in_bucket(key=path)

            # If path exists, check if local paths exist  
            if not file_in_bucket:
                return f"File does not exist: {path}, " \
                    "not downloading anything."
            else:
                for f in filelist:
                    new_path = self.tempdir[1] / Path(f)
                    # If the local paths don't exist create them 
                    if not new_path.parent.exists():
                        try:
                            new_path.parent.mkdir(parents=True)
                        except IOError as ioe:
                            sys.exit("Could not create folder "
                                     f"{new_path.parent}. Cannot"
                                     "proceed with delivery. Cancelling: "
                                     f"{ioe}")

                    # If the file doesn't exist locally, download to path
                    if not new_path.exists():
                        try:
                            self.s3.resource.meta.client.download_file(
                                self.s3.bucket.name,
                                f, str(new_path)
                            )
                        except Exception as e:
                            print(f"Download of file {f} failed.")
                        else:
                            return f"Success: {str(new_path)} downloaded " \
                                "from S3 to folder '{path}'!"
                    else:
                        print(f"File {str(new_path)} already exists. "
                              "Not downloading.")

import threading

class ProgressPercentage(object):

    def __init__(self, filename):
        self._filename = filename
        self._size = float(os.path.getsize(filename))
        self._seen_so_far = 0
        self._lock = threading.Lock()

    def __call__(self, bytes_amount):
        # To simplify, assume this is hooked up to a single filename
        with self._lock:
            self._seen_so_far += bytes_amount
            percentage = (self._seen_so_far / self._size) * 100
            sys.stdout.write(
                "\r%s  %s / %s  (%.2f%%)" % (
                    self._filename, self._seen_so_far, self._size,
                    percentage))
            sys.stdout.flush()