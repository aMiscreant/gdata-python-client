#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tool for uploading diffs from a version control system to the codereview app.

Usage summary: upload.py [options] [-- diff_options]

Diff options are passed to the diff command of the underlying system.

Supported version control systems:
  Git
  Mercurial
  Subversion

It is important for Git/Mercurial users to specify a tree/node/branch to diff
against by using the '--rev' option.
"""
# This code is derived from appcfg.py in the App Engine SDK (open source),
# and from ASPN recipe #146306.
import mimetypes
import sys
import os
import subprocess
import logging
import sys
import argparse
import fnmatch
import getpass
import logging
import mimetypes
import os
import re
import socket
import subprocess
import sys
import urllib
from hashlib import md5
from http.cookiejar import MozillaCookieJar, CookieJar
from urllib.parse import urlparse

import urllib3
from urllib3 import PoolManager, exceptions

try:
  import readline
except ImportError:
  pass

# The logging verbosity:
#  0: Errors only.
#  1: Status messages.
#  2: Info logs.
#  3: Debug logs.
verbosity = 1

# Max size of patch or base file.
MAX_UPLOAD_SIZE = 900 * 1024

# Constants for version control names.  Used by GuessVCSName.
VCS_GIT = "Git"
VCS_MERCURIAL = "Mercurial"
VCS_SUBVERSION = "Subversion"
VCS_UNKNOWN = "Unknown"

# whitelist for non-binary filetypes which do not start with "text/"
# .mm (Objective-C) shows up as application/x-freemind on my Linux box.
TEXT_MIMETYPES = ['application/javascript', 'application/x-javascript',
                  'application/x-freemind']

# TEXT_MIMETYPES = {"application/xml", "application/json"}

VCS_ABBREVIATIONS = {
  VCS_MERCURIAL.lower(): VCS_MERCURIAL,
  "hg": VCS_MERCURIAL,
  VCS_SUBVERSION.lower(): VCS_SUBVERSION,
  "svn": VCS_SUBVERSION,
  VCS_GIT.lower(): VCS_GIT,
}

# The result of parsing Subversion's [auto-props] setting.
svn_auto_props_map = None

def get_email(prompt):
    """Prompts the user for their email address and returns it.

    The last used email address is saved to a file and offered up as a suggestion
    to the user. If the user presses enter without typing in anything the last
    used email address is used. If the user enters a new address, it is saved
    for next time we prompt.

    """
    last_email_file_name = os.path.expanduser("~/.last_codereview_email_address")
    last_email = ""
    if os.path.exists(last_email_file_name):
        try:
            with open(last_email_file_name, "r") as last_email_file:
                last_email = last_email_file.readline().strip("\n")
            prompt += f" [{last_email}]"
        except IOError:
            pass

    # Use input() in Python 3
    email = input(prompt + ": ").strip()
    if email:
        try:
            with open(last_email_file_name, "w") as last_email_file:
                last_email_file.write(email)
        except IOError:
            pass
    else:
        email = last_email
    return email



def status_update(msg):
    """Print a status message to stdout.

    If 'verbosity' is greater than 0, print the message.

    Args:
        msg: The string to print.
    """
    if verbosity > 0:
        print(msg)


def error_exit(msg):
    """Print an error message to stderr and exit.

    Args:
        msg: The error message to print.
    """
    print(msg, file=sys.stderr)
    sys.exit(1)


class ClientLoginError(exceptions.HTTPError):
    """Raised to indicate there was an error authenticating with ClientLogin."""

    def __init__(self, url, code, msg, headers, args):
        super().__init__(f"HTTP Error {code}: {msg}")
        self.url = url
        self.code = code
        self.msg = msg
        self.headers = headers
        self.args = args
        self.reason = args["Error"]

class AbstractRpcServer(object):
    """Provides a common interface for a simple RPC server."""

    def __init__(self, host, auth_function, host_override=None, extra_headers=None,
                 save_cookies=False):
        """Creates a new HttpRpcServer.

        Args:
            host: The host to send requests to.
            auth_function: A function that takes no arguments and returns an
                (email, password) tuple when called. Will be called if authentication
                is required.
            host_override: The host header to send to the server (defaults to host).
            extra_headers: A dict of extra headers to append to every request.
            save_cookies: If True, save the authentication cookies to local disk.
                If False, use an in-memory cookiejar instead. Defaults to False.
        """
        self.host = host
        self.host_override = host_override
        self.auth_function = auth_function
        self.authenticated = False
        self.extra_headers = extra_headers if extra_headers is not None else {}
        self.save_cookies = save_cookies
        self.http = PoolManager()
        if self.host_override:
            logging.info("Server: %s; Host: %s", self.host, self.host_override)
        else:
            logging.info("Server: %s", self.host)

    def _GetOpener(self):
        """Returns an OpenerDirector for making HTTP requests.

        Returns:
          An object for making HTTP requests.
        """
        raise NotImplementedError()

    def _CreateRequest(self, url, data=None):
        """Creates a new HTTP request.

        Args:
            url: The URL for the request.
            data: Optional data payload for the request.

        Returns:
            A request object with added headers.
        """
        logging.debug("Creating request for: '%s' with payload:\n%s", url, data)
        headers = {"Host": self.host_override} if self.host_override else {}
        headers.update(self.extra_headers)
        req = urllib3.request.RequestMethods().request_encode_body('POST' if data else 'GET', url, body=data,
                                                                   headers=headers)
        return req

    def _GetAuthToken(self, email, password):
        """Uses ClientLogin to authenticate the user, returning an auth token.

        Args:
            email: The user's email address.
            password: The user's password.

        Raises:
            ClientLoginError: If there was an error authenticating with ClientLogin.
            HTTPError: If there was some other form of HTTP error.

        Returns:
            The authentication token returned by ClientLogin.
        """
        account_type = "GOOGLE"
        if self.host.endswith(".google.com"):
            account_type = "HOSTED"
        req = self._CreateRequest(
            url="https://www.google.com/accounts/ClientLogin",
            data=urllib3.request.urlencode({
                "Email": email,
                "Passwd": password,
                "service": "ah",
                "source": "rietveld-codereview-upload",
                "accountType": account_type,
            }).encode('utf-8'),
        )
        try:
            response = self.http.urlopen('POST', req.get_full_url(), body=req.data, headers=req.headers)
            response_body = response.data.decode('utf-8')
            response_dict = dict(x.split("=") for x in response_body.split("\n") if x)
            return response_dict["Auth"]
        except urllib3.exceptions.HTTPError as e:
            if e.status == 403:
                body = e.data.decode('utf-8')
                response_dict = dict(x.split("=", 1) for x in body.split("\n") if x)
                raise ClientLoginError(req.get_full_url(), e.status, e.reason, e.headers, response_dict)
            else:
                raise

    def _GetAuthCookie(self, auth_token):
        """Fetches authentication cookies for an authentication token.

        Args:
            auth_token: The authentication token returned by ClientLogin.

        Raises:
            HTTPError: If there was an error fetching the authentication cookies.
        """
        continue_location = "http://localhost/"
        args = {"continue": continue_location, "auth": auth_token}
        req = self._CreateRequest(f"http://{self.host}/_ah/login?{urllib3.request.urlencode(args)}")
        try:
            response = self.http.urlopen('GET', req.get_full_url(), headers=req.headers)
        except urllib3.exceptions.HTTPError as e:
            response = e
        if response.status != 302 or response.getheader("location") != continue_location:
            raise urllib3.exceptions.HTTPError(req.get_full_url(), response.status, response.reason, response.headers, response.data)
        self.authenticated = True

    def _Authenticate(self):
        """Authenticates the user.

        The authentication process works as follows:
         1) We get a username and password from the user
         2) We use ClientLogin to obtain an AUTH token for the user
            (see http://code.google.com/apis/accounts/AuthForInstalledApps.html).
         3) We pass the auth token to /_ah/login on the server to obtain an
            authentication cookie. If login was successful, it tries to redirect
            us to the URL we provided.

        If we attempt to access the upload API without first obtaining an
        authentication cookie, it returns a 401 response (or a 302) and
        directs us to authenticate ourselves with ClientLogin.
        """
        for i in range(3):
            credentials = self.auth_function()
            try:
                auth_token = self._GetAuthToken(credentials[0], credentials[1])
            except ClientLoginError as e:
                if e.reason == "BadAuthentication":
                    print("Invalid username or password.", file=sys.stderr)
                    continue
                if e.reason == "CaptchaRequired":
                    print(
                        "Please go to\n"
                        "https://www.google.com/accounts/DisplayUnlockCaptcha\n"
                        "and verify you are a human.  Then try again.",
                        file=sys.stderr
                    )
                    break
                if e.reason == "NotVerified":
                    print("Account not verified.", file=sys.stderr)
                    break
                if e.reason == "TermsNotAgreed":
                    print("User has not agreed to TOS.", file=sys.stderr)
                    break
                if e.reason == "AccountDeleted":
                    print("The user account has been deleted.", file=sys.stderr)
                    break
                if e.reason == "AccountDisabled":
                    print("The user account has been disabled.", file=sys.stderr)
                    break
                if e.reason == "ServiceDisabled":
                    print("The user's access to the service has been disabled.", file=sys.stderr)
                    break
                if e.reason == "ServiceUnavailable":
                    print("The service is not available; try again later.", file=sys.stderr)
                    break
                raise
            self._GetAuthCookie(auth_token)
            return

    def Send(self, request_path, payload=None,
             content_type="application/octet-stream",
             timeout=None,
             **kwargs):
        """Sends an RPC and returns the response.

        Args:
            request_path: The path to send the request to, eg /api/appversion/create.
            payload: The body of the request, or None to send an empty request.
            content_type: The Content-Type header to use.
            timeout: timeout in seconds; default None i.e. no timeout.
                (Note: for large requests on OS X, the timeout doesn't work right.)
            kwargs: Any keyword arguments are converted into query string parameters.

        Returns:
            The response body, as a string.
        """
        if not self.authenticated:
            self._Authenticate()

        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            tries = 0
            while True:
                tries += 1
                args = dict(kwargs)
                url = f"http://{self.host}{request_path}"
                if args:
                    url += "?" + urllib3.request.urlencode(args)
                req = self._CreateRequest(url=url, data=payload)
                req.add_header("Content-Type", content_type)
                try:
                    response = self.http.urlopen('POST' if payload else 'GET', url, body=payload, headers=req.headers)
                    return response.data.decode('utf-8')
                except urllib3.exceptions.HTTPError as e:
                    if tries > 3 or e.status not in (401, 302):
                        raise
                    self._Authenticate()
        finally:
            socket.setdefaulttimeout(old_timeout)


class HttpRpcServer(AbstractRpcServer):
    """Provides a simplified RPC-style interface for HTTP requests."""

    def _Authenticate(self):
        """Save the cookie jar after authentication."""
        super(HttpRpcServer, self)._Authenticate()
        if self.save_cookies:
            status_update("Saving authentication cookies to %s" % self.cookie_file)
            self.cookie_jar.save()

    def _GetOpener(self):
        """Sets up an HTTP opener that supports cookies.

        Returns:
            A urllib3 PoolManager object.
        """
        self.http = urllib3.PoolManager()
        if self.save_cookies:
            self.cookie_file = os.path.expanduser("~/.codereview_upload_cookies")
            self.cookie_jar = MozillaCookieJar(self.cookie_file)
            if os.path.exists(self.cookie_file):
                try:
                    self.cookie_jar.load()
                    self.authenticated = True
                    status_update("Loaded authentication cookies from %s" % self.cookie_file)
                except (Exception, IOError):
                    pass
            else:
                fd = os.open(self.cookie_file, os.O_CREAT, 0o600)
                os.close(fd)
            os.chmod(self.cookie_file, 0o600)
        else:
            self.cookie_jar = CookieJar()

        return self.http


parser = argparse.ArgumentParser(
    description="Process command-line arguments for gdata-python-client.",
    usage="%(prog)s [options] [-- diff_options]"
)

parser.add_argument("-y", "--assume_yes", action="store_true",
                    help="Assume that the answer to yes/no questions is 'yes'.")

# Logging
logging_group = parser.add_argument_group("Logging options")
logging_group.add_argument("-q", "--quiet", action="store_const", const=0,
                           dest="verbose", help="Print errors only.")
logging_group.add_argument("-v", "--verbose", action="store_const", const=2,
                           dest="verbose", default=1,
                           help="Print info level logs (default).")
logging_group.add_argument("--noisy", action="store_const", const=3,
                           dest="verbose", help="Print all logs.")

# Review server
server_group = parser.add_argument_group("Review server options")
server_group.add_argument("-s", "--server", dest="server",
                          default="codereview.appspot.com",
                          metavar="SERVER",
                          help=("The server to upload to. The format is host[:port]. "
                                "Defaults to '%(default)s'."))
server_group.add_argument("-e", "--email", dest="email",
                          metavar="EMAIL", default=None,
                          help="The username to use. Will prompt if omitted.")
server_group.add_argument("-H", "--host", dest="host",
                          metavar="HOST", default=None,
                          help="Overrides the Host header sent with all RPCs.")
server_group.add_argument("--no_cookies", action="store_false",
                          dest="save_cookies", default=True,
                          help="Do not save authentication cookies to local disk.")

# Issue options
issue_group = parser.add_argument_group("Issue options")
issue_group.add_argument("-d", "--description", dest="description",
                         metavar="DESCRIPTION", default=None,
                         help="Optional description when creating an issue.")
issue_group.add_argument("-f", "--description_file", dest="description_file",
                         metavar="DESCRIPTION_FILE", default=None,
                         help="Optional path of a file that contains "
                              "the description when creating an issue.")
issue_group.add_argument("-r", "--reviewers", dest="reviewers",
                         metavar="REVIEWERS", default=",afshar@google.com",
                         help="Add reviewers (comma separated email addresses).")
issue_group.add_argument("--cc", dest="cc",
                         metavar="CC", default="gdata-python-client-library-contributors@googlegroups.com",
                         help="Add CC (comma separated email addresses).")
issue_group.add_argument("--private", action="store_true", dest="private",
                         default=False,
                         help="Make the issue restricted to reviewers and those CCed.")

# Upload options
upload_group = parser.add_argument_group("Patch options")
upload_group.add_argument("-m", "--message", dest="message",
                          metavar="MESSAGE", default=None,
                          help="A message to identify the patch. "
                               "Will prompt if omitted.")
upload_group.add_argument("-i", "--issue", type=int, dest="issue",
                          metavar="ISSUE", default=None,
                          help="Issue number to which to add. Defaults to new issue.")
upload_group.add_argument("--base_url", dest="base_url", default=None,
                          help="Base repository URL (listed as \"Base URL\" when "
                               "viewing issue). If omitted, will be guessed automatically "
                               "for SVN repos and left blank for others.")
upload_group.add_argument("--download_base", action="store_true",
                          dest="download_base", default=False,
                          help="Base files will be downloaded by the server "
                               "(side-by-side diffs may not work on files with CRs).")
upload_group.add_argument("--rev", dest="revision",
                          metavar="REV", default=None,
                          help="Base revision/branch/tree to diff against. Use "
                               "rev1:rev2 range to review already committed changeset.")
upload_group.add_argument("--send_mail", action="store_true",
                          dest="send_mail", default=True,
                          help="Send notification email to reviewers.")
upload_group.add_argument("--vcs", dest="vcs",
                          metavar="VCS", default=None,
                          help=("Version control system (optional, usually upload.py "
                                "already guesses the right VCS)."))
upload_group.add_argument("--emulate_svn_auto_props", action="store_true",
                          dest="emulate_svn_auto_props", default=False,
                          help="Emulate Subversion's auto properties feature.")

args = parser.parse_args()

import getpass
import logging

def get_rpc_server(options):
    """Returns an instance of an AbstractRpcServer.

    Returns:
        A new AbstractRpcServer, on which RPC calls can be made.
    """

    rpc_server_class = HttpRpcServer

    def get_user_credentials():
        """Prompts the user for a username and password."""
        email = options.email
        if email is None:
            email = get_email(f"Email (login for uploading to {options.server})")
        password = getpass.getpass(f"Password for {email}: ")
        return (email, password)

    # If this is the dev_appserver, use fake authentication.
    host = (options.host or options.server).lower()
    if host == "localhost" or host.startswith("localhost:"):
        email = options.email
        if email is None:
            email = "test@example.com"
            logging.info("Using debug user %s. Override with --email", email)
        server = rpc_server_class(
            options.server,
            lambda: (email, "password"),
            host_override=options.host,
            extra_headers={"Cookie": f'dev_appserver_login="{email}:False"'},
            save_cookies=options.save_cookies
        )
        # Skip ClientLogin for localhost testing
        server.authenticated = True
        return server

    return rpc_server_class(
        options.server,
        get_user_credentials,
        host_override=options.host,
        save_cookies=options.save_cookies
    )

def encode_multipart_form_data(fields, files):
    """Encode form fields for multipart/form-data.

    Args:
        fields: A sequence of (name, value) elements for regular form fields.
        files: A sequence of (name, filename, value) elements for data to be
               uploaded as files.
    Returns:
        (content_type, body) ready for urllib3 HTTP request.
    """
    BOUNDARY = '-M-A-G-I-C---B-O-U-N-D-A-R-Y-'
    CRLF = '\r\n'
    lines = []

    # Add fields
    for key, value in fields:
        lines.append(f'--{BOUNDARY}')
        lines.append(f'Content-Disposition: form-data; name="{key}"')
        lines.append('')
        lines.append(value)

    # Add files
    for key, filename, value in files:
        lines.append(f'--{BOUNDARY}')
        lines.append(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"')
        lines.append(f'Content-Type: {get_content_type(filename)}')
        lines.append('')
        lines.append(value)

    # End of the boundary
    lines.append(f'--{BOUNDARY}--')
    lines.append('')
    body = CRLF.join(lines)
    content_type = f'multipart/form-data; boundary={BOUNDARY}'
    return content_type, body


def get_content_type(filename):
    """Helper to guess the content-type from the filename."""
    return mimetypes.guess_type(filename)[0] or 'application/octet-stream'


# Use a shell for subcommands on Windows to get a PATH search.
use_shell = sys.platform.startswith("win")

def run_shell_with_return_code(command, print_output=False,
                               universal_newlines=True,
                               env=os.environ):
    """Executes a command and returns the output from stdout and the return code.

    Args:
        command: Command to execute.
        print_output: If True, the output is printed to stdout.
                      If False, both stdout and stderr are ignored.
        universal_newlines: Use universal_newlines flag (default: True).

    Returns:
        Tuple (output, return code)
    """
    logging.info("Running %s", command)
    p = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=use_shell,
        universal_newlines=universal_newlines,
        env=env
    )

    if print_output:
        output_lines = []
        for line in iter(p.stdout.readline, ''):
            print(line.strip())
            output_lines.append(line)
        output = "".join(output_lines)
    else:
        output = p.stdout.read()

    p.wait()
    errout = p.stderr.read()

    if print_output and errout:
        print(errout, file=sys.stderr)

    p.stdout.close()
    p.stderr.close()

    return output, p.returncode

def run_shell(command, silent_ok=False, universal_newlines=True,
              print_output=False, env=os.environ):
    data, retcode = run_shell_with_return_code(command, print_output,
                                               universal_newlines, env)
    if retcode:
        error_exit(f"Got error status from {command}:\n{data}")
    if not silent_ok and not data:
        error_exit(f"No output from {command}")
    return data

class VersionControlSystem(object):
    """Abstract base class providing an interface to the VCS."""

    def __init__(self, options):
        """Constructor.

        Args:
            options: Command line options.
        """
        self.options = options

    def generate_diff(self, args):
        """Return the current diff as a string.

        Args:
            args: Extra arguments to pass to the diff command.
        """
        raise NotImplementedError(
            f"abstract method -- subclass {self.__class__.__name__} must override"
        )

    def get_unknown_files(self):
        """Return a list of files unknown to the VCS."""
        raise NotImplementedError(
            f"abstract method -- subclass {self.__class__.__name__} must override"
        )

    def check_for_unknown_files(self):
        """Show an "are you sure?" prompt if there are unknown files."""
        unknown_files = self.get_unknown_files()
        if unknown_files:
            print("The following files are not added to version control:")
            for line in unknown_files:
                print(line)
            prompt = "Are you sure to continue? (y/N) "
            answer = input(prompt).strip().lower()
            if answer != "y":
                error_exit("User aborted")

    def get_base_file(self, filename):
        """Get the content of the upstream version of a file.

        Returns:
            A tuple (base_content, new_content, is_binary, status)
              base_content: The contents of the base file.
              new_content: For text files, this is empty. For binary files, this is
                the contents of the new file, since the diff output won't contain
                information to reconstruct the current file.
              is_binary: True if the file is binary.
              status: The status of the file.
        """
        raise NotImplementedError(
            f"abstract method -- subclass {self.__class__.__name__} must override"
        )

    def get_base_files(self, diff):
        """Helper that calls get_base_file for each file in the patch.

        Returns:
            A dictionary that maps from filename to get_base_file's tuple.
        """
        files = {}
        for line in diff.splitlines(True):
            if line.startswith('Index:') or line.startswith('Property changes on:'):
                _, filename = line.split(':', 1)
                filename = filename.strip().replace('\\', '/')
                files[filename] = self.get_base_file(filename)
        return files

    def upload_base_files(self, issue, rpc_server, patch_list, patchset, options, files):
        """Uploads the base files (and if necessary, the current ones as well)."""

        def upload_file(filename, file_id, content, is_binary, status, is_base):
            """Uploads a file to the server."""
            file_too_large = False
            file_type = "base" if is_base else "current"
            if len(content) > MAX_UPLOAD_SIZE:
                print(f"Not uploading the {file_type} file for {filename} because it's too large.")
                file_too_large = True
                content = ""
            checksum = md5(content.encode()).hexdigest()
            if options.verbose > 0 and not file_too_large:
                print(f"Uploading {file_type} file for {filename}")
            url = f"/{int(issue)}/upload_content/{int(patchset)}/{file_id}"
            form_fields = [
                ("filename", filename),
                ("status", status),
                ("checksum", checksum),
                ("is_binary", str(is_binary)),
                ("is_current", str(not is_base)),
            ]
            if file_too_large:
                form_fields.append(("file_too_large", "1"))
            if options.email:
                form_fields.append(("user", options.email))
            ctype, body = encode_multipart_form_data(form_fields, [("data", filename, content)])
            response_body = rpc_server.send(url, body, content_type=ctype)
            if not response_body.startswith("OK"):
                status_update(f"  --> {response_body}")
                sys.exit(1)

        patches = {v: k for k, v in patch_list}
        for filename, file_id_str in patches.items():
            base_content, new_content, is_binary, status = files[filename]
            if "nobase" in file_id_str:
                base_content = None
                file_id_str = file_id_str[file_id_str.rfind("_") + 1:]
            file_id = int(file_id_str)
            if base_content is not None:
                upload_file(filename, file_id, base_content, is_binary, status, True)
            if new_content is not None:
                upload_file(filename, file_id, new_content, is_binary, status, False)

    def is_image(self, filename):
        """Returns true if the filename has an image extension."""
        mimetype = mimetypes.guess_type(filename)[0]
        return mimetype is not None and mimetype.startswith("image/")

    def is_binary(self, filename):
        """Returns true if the guessed mimetype isn't in the text group."""
        mimetype = mimetypes.guess_type(filename)[0]
        if not mimetype:
            return False
        if mimetype in TEXT_MIMETYPES:
            return False
        return not mimetype.startswith("text/")


class SubversionVCS(VersionControlSystem):
  """Implementation of the VersionControlSystem interface for Subversion."""

  def __init__(self, options):
    super(SubversionVCS, self).__init__(options)
    if self.options.revision:
      match = re.match(r"(\d+)(:(\d+))?", self.options.revision)
      if not match:
        ErrorExit("Invalid Subversion revision %s." % self.options.revision)
      self.rev_start = match.group(1)
      self.rev_end = match.group(3)
    else:
      self.rev_start = self.rev_end = None
    # Cache output from "svn list -r REVNO dirname".
    # Keys: dirname, Values: 2-tuple (ouput for start rev and end rev).
    self.svnls_cache = {}
    # Base URL is required to fetch files deleted in an older revision.
    # Result is cached to not guess it over and over again in GetBaseFile().
    required = self.options.download_base or self.options.revision is not None
    self.svn_base = self._GuessBase(required)

  def GuessBase(self, required):
    """Wrapper for _GuessBase."""
    return self.svn_base

  def _GuessBase(self, required):
    """Returns the SVN base URL.

    Args:
      required: If true, exits if the url can't be guessed, otherwise None is
        returned.
    """
    info = run_shell(["svn", "info"])
    for line in info.splitlines():
      words = line.split()
      if len(words) == 2 and words[0] == "URL:":
        url = words[1]
        scheme, netloc, path, params, query, fragment = urlparse.urlparse(url)
        username, netloc = urllib.splituser(netloc)
        if username:
          logging.info("Removed username from base URL")
        if netloc.endswith("svn.python.org"):
          if netloc == "svn.python.org":
            if path.startswith("/projects/"):
              path = path[9:]
          elif netloc != "pythondev@svn.python.org":
            ErrorExit("Unrecognized Python URL: %s" % url)
          base = "http://svn.python.org/view/*checkout*%s/" % path
          logging.info("Guessed Python base = %s", base)
        elif netloc.endswith("svn.collab.net"):
          if path.startswith("/repos/"):
            path = path[6:]
          base = "http://svn.collab.net/viewvc/*checkout*%s/" % path
          logging.info("Guessed CollabNet base = %s", base)
        elif netloc.endswith(".googlecode.com"):
          path = path + "/"
          base = urlparse.urlunparse(("http", netloc, path, params,
                                      query, fragment))
          logging.info("Guessed Google Code base = %s", base)
        else:
          path = path + "/"
          base = urlparse.urlunparse((scheme, netloc, path, params,
                                      query, fragment))
          logging.info("Guessed base = %s", base)
        return base
    if required:
      ErrorExit("Can't find URL in output from svn info")
    return None

  def GenerateDiff(self, args):
    cmd = ["svn", "diff"]
    if self.options.revision:
      cmd += ["-r", self.options.revision]
    cmd.extend(args)
    data = run_shell(cmd)
    count = 0
    for line in data.splitlines():
      if line.startswith("Index:") or line.startswith("Property changes on:"):
        count += 1
        logging.info(line)
    if not count:
      ErrorExit("No valid patches found in output from svn diff")
    return data

  def _CollapseKeywords(self, content, keyword_str):
    """Collapses SVN keywords."""
    # svn cat translates keywords but svn diff doesn't. As a result of this
    # behavior patching.PatchChunks() fails with a chunk mismatch error.
    # This part was originally written by the Review Board development team
    # who had the same problem (http://reviews.review-board.org/r/276/).
    # Mapping of keywords to known aliases
    svn_keywords = {
      # Standard keywords
      'Date':                ['Date', 'LastChangedDate'],
      'Revision':            ['Revision', 'LastChangedRevision', 'Rev'],
      'Author':              ['Author', 'LastChangedBy'],
      'HeadURL':             ['HeadURL', 'URL'],
      'Id':                  ['Id'],

      # Aliases
      'LastChangedDate':     ['LastChangedDate', 'Date'],
      'LastChangedRevision': ['LastChangedRevision', 'Rev', 'Revision'],
      'LastChangedBy':       ['LastChangedBy', 'Author'],
      'URL':                 ['URL', 'HeadURL'],
    }

    def repl(m):
       if m.group(2):
         return "$%s::%s$" % (m.group(1), " " * len(m.group(3)))
       return "$%s$" % m.group(1)
    keywords = [keyword
                for name in keyword_str.split(" ")
                for keyword in svn_keywords.get(name, [])]
    return re.sub(r"\$(%s):(:?)([^\$]+)\$" % '|'.join(keywords), repl, content)

  def GetUnknownFiles(self):
    status = run_shell(["svn", "status", "--ignore-externals"], silent_ok=True)
    unknown_files = []
    for line in status.split("\n"):
      if line and line[0] == "?":
        unknown_files.append(line)
    return unknown_files

  def ReadFile(self, filename):
    """Returns the contents of a file."""
    file = open(filename, 'rb')
    result = ""
    try:
      result = file.read()
    finally:
      file.close()
    return result

  def GetStatus(self, filename):
    """Returns the status of a file."""
    if not self.options.revision:
      status = run_shell(["svn", "status", "--ignore-externals", filename])
      if not status:
        ErrorExit("svn status returned no output for %s" % filename)
      status_lines = status.splitlines()
      # If file is in a cl, the output will begin with
      # "\n--- Changelist 'cl_name':\n".  See
      # http://svn.collab.net/repos/svn/trunk/notes/changelist-design.txt
      if (len(status_lines) == 3 and
          not status_lines[0] and
          status_lines[1].startswith("--- Changelist")):
        status = status_lines[2]
      else:
        status = status_lines[0]
    # If we have a revision to diff against we need to run "svn list"
    # for the old and the new revision and compare the results to get
    # the correct status for a file.
    else:
      dirname, relfilename = os.path.split(filename)
      if dirname not in self.svnls_cache:
        cmd = ["svn", "list", "-r", self.rev_start, dirname or "."]
        out, returncode = run_shell_with_return_code(cmd)
        if returncode:
          ErrorExit("Failed to get status for %s." % filename)
        old_files = out.splitlines()
        args = ["svn", "list"]
        if self.rev_end:
          args += ["-r", self.rev_end]
        cmd = args + [dirname or "."]
        out, returncode = run_shell_with_return_code(cmd)
        if returncode:
          ErrorExit("Failed to run command %s" % cmd)
        self.svnls_cache[dirname] = (old_files, out.splitlines())
      old_files, new_files = self.svnls_cache[dirname]
      if relfilename in old_files and relfilename not in new_files:
        status = "D   "
      elif relfilename in old_files and relfilename in new_files:
        status = "M   "
      else:
        status = "A   "
    return status

  def GetBaseFile(self, filename):
    status = self.GetStatus(filename)
    base_content = None
    new_content = None

    # If a file is copied its status will be "A  +", which signifies
    # "addition-with-history".  See "svn st" for more information.  We need to
    # upload the original file or else diff parsing will fail if the file was
    # edited.
    if status[0] == "A" and status[3] != "+":
      # We'll need to upload the new content if we're adding a binary file
      # since diff's output won't contain it.
      mimetype = run_shell(["svn", "propget", "svn:mime-type", filename],
                          silent_ok=True)
      base_content = ""
      is_binary = bool(mimetype) and not mimetype.startswith("text/")
      if is_binary and self.IsImage(filename):
        new_content = self.ReadFile(filename)
    elif (status[0] in ("M", "D", "R") or
          (status[0] == "A" and status[3] == "+") or  # Copied file.
          (status[0] == " " and status[1] == "M")):  # Property change.
      args = []
      if self.options.revision:
        url = "%s/%s@%s" % (self.svn_base, filename, self.rev_start)
      else:
        # Don't change filename, it's needed later.
        url = filename
        args += ["-r", "BASE"]
      cmd = ["svn"] + args + ["propget", "svn:mime-type", url]
      mimetype, returncode = run_shell_with_return_code(cmd)
      if returncode:
        # File does not exist in the requested revision.
        # Reset mimetype, it contains an error message.
        mimetype = ""
      get_base = False
      is_binary = bool(mimetype) and not mimetype.startswith("text/")
      if status[0] == " ":
        # Empty base content just to force an upload.
        base_content = ""
      elif is_binary:
        if self.IsImage(filename):
          get_base = True
          if status[0] == "M":
            if not self.rev_end:
              new_content = self.ReadFile(filename)
            else:
              url = "%s/%s@%s" % (self.svn_base, filename, self.rev_end)
              new_content = run_shell(["svn", "cat", url],
                                     universal_newlines=True, silent_ok=True)
        else:
          base_content = ""
      else:
        get_base = True

      if get_base:
        if is_binary:
          universal_newlines = False
        else:
          universal_newlines = True
        if self.rev_start:
          # "svn cat -r REV delete_file.txt" doesn't work. cat requires
          # the full URL with "@REV" appended instead of using "-r" option.
          url = "%s/%s@%s" % (self.svn_base, filename, self.rev_start)
          base_content = run_shell(["svn", "cat", url],
                                  universal_newlines=universal_newlines,
                                  silent_ok=True)
        else:
          base_content = run_shell(["svn", "cat", filename],
                                  universal_newlines=universal_newlines,
                                  silent_ok=True)
        if not is_binary:
          args = []
          if self.rev_start:
            url = "%s/%s@%s" % (self.svn_base, filename, self.rev_start)
          else:
            url = filename
            args += ["-r", "BASE"]
          cmd = ["svn"] + args + ["propget", "svn:keywords", url]
          keywords, returncode = run_shell_with_return_code(cmd)
          if keywords and not returncode:
            base_content = self._CollapseKeywords(base_content, keywords)
    else:
      status_update("svn status returned unexpected output: %s" % status)
      sys.exit(1)
    return base_content, new_content, is_binary, status[0:5]


class GitVCS(VersionControlSystem):
  """Implementation of the VersionControlSystem interface for Git."""

  def __init__(self, options):
    super(GitVCS, self).__init__(options)
    # Map of filename -> (hash before, hash after) of base file.
    # Hashes for "no such file" are represented as None.
    self.hashes = {}
    # Map of new filename -> old filename for renames.
    self.renames = {}

  def GenerateDiff(self, extra_args):
    # This is more complicated than svn's GenerateDiff because we must convert
    # the diff output to include an svn-style "Index:" line as well as record
    # the hashes of the files, so we can upload them along with our diff.

    # Special used by git to indicate "no such content".
    NULL_HASH = "0"*40

    extra_args = extra_args[:]
    if self.options.revision:
      extra_args = [self.options.revision] + extra_args

    # --no-ext-diff is broken in some versions of Git, so try to work around
    # this by overriding the environment (but there is still a problem if the
    # git config key "diff.external" is used).
    env = os.environ.copy()
    if 'GIT_EXTERNAL_DIFF' in env: del env['GIT_EXTERNAL_DIFF']
    gitdiff = run_shell(["git", "diff", "--no-ext-diff", "--full-index", "-M"]
                       + extra_args, env=env)

    def IsFileNew(filename):
      return filename in self.hashes and self.hashes[filename][0] is None

    def AddSubversionPropertyChange(filename):
      """Add svn's property change information into the patch if given file is
      new file.

      We use Subversion's auto-props setting to retrieve its property.
      See http://svnbook.red-bean.com/en/1.1/ch07.html#svn-ch-7-sect-1.3.2 for
      Subversion's [auto-props] setting.
      """
      if self.options.emulate_svn_auto_props and IsFileNew(filename):
        svnprops = GetSubversionPropertyChanges(filename)
        if svnprops:
          svndiff.append("\n" + svnprops + "\n")

    svndiff = []
    filecount = 0
    filename = None
    for line in gitdiff.splitlines():
      match = re.match(r"diff --git a/(.*) b/(.*)$", line)
      if match:
        # Add auto property here for previously seen file.
        if filename is not None:
          AddSubversionPropertyChange(filename)
        filecount += 1
        # Intentionally use the "after" filename so we can show renames.
        filename = match.group(2)
        svndiff.append("Index: %s\n" % filename)
        if match.group(1) != match.group(2):
          self.renames[match.group(2)] = match.group(1)
      else:
        # The "index" line in a git diff looks like this (long hashes elided):
        #   index 82c0d44..b2cee3f 100755
        # We want to save the left hash, as that identifies the base file.
        match = re.match(r"index (\w+)\.\.(\w+)", line)
        if match:
          before, after = (match.group(1), match.group(2))
          if before == NULL_HASH:
            before = None
          if after == NULL_HASH:
            after = None
          self.hashes[filename] = (before, after)
      svndiff.append(line + "\n")
    if not filecount:
      ErrorExit("No valid patches found in output from git diff")
    # Add auto property for the last seen file.
    assert filename is not None
    AddSubversionPropertyChange(filename)
    return "".join(svndiff)

  def GetUnknownFiles(self):
    status = run_shell(["git", "ls-files", "--exclude-standard", "--others"],
                      silent_ok=True)
    return status.splitlines()

  def GetFileContent(self, file_hash, is_binary):
    """Returns the content of a file identified by its git hash."""
    data, retcode = run_shell_with_return_code(["git", "show", file_hash],
                                            universal_newlines=not is_binary)
    if retcode:
      ErrorExit("Got error status from 'git show %s'" % file_hash)
    return data

  def GetBaseFile(self, filename):
    hash_before, hash_after = self.hashes.get(filename, (None,None))
    base_content = None
    new_content = None
    is_binary = self.IsBinary(filename)
    status = None

    if filename in self.renames:
      status = "A +"  # Match svn attribute name for renames.
      if filename not in self.hashes:
        # If a rename doesn't change the content, we never get a hash.
        base_content = run_shell(["git", "show", "HEAD:" + filename])
    elif not hash_before:
      status = "A"
      base_content = ""
    elif not hash_after:
      status = "D"
    else:
      status = "M"

    is_image = self.IsImage(filename)

    # Grab the before/after content if we need it.
    # We should include file contents if it's text or it's an image.
    if not is_binary or is_image:
      # Grab the base content if we don't have it already.
      if base_content is None and hash_before:
        base_content = self.GetFileContent(hash_before, is_binary)
      # Only include the "after" file if it's an image; otherwise it
      # it is reconstructed from the diff.
      if is_image and hash_after:
        new_content = self.GetFileContent(hash_after, is_binary)

    return (base_content, new_content, is_binary, status)


class MercurialVCS(VersionControlSystem):
  """Implementation of the VersionControlSystem interface for Mercurial."""

  def __init__(self, options, repo_dir):
    super(MercurialVCS, self).__init__(options)
    # Absolute path to repository (we can be in a subdir)
    self.repo_dir = os.path.normpath(repo_dir)
    # Compute the subdir
    cwd = os.path.normpath(os.getcwd())
    assert cwd.startswith(self.repo_dir)
    self.subdir = cwd[len(self.repo_dir):].lstrip(r"\/")
    if self.options.revision:
      self.base_rev = self.options.revision
    else:
      self.base_rev = run_shell(["hg", "parent", "-q"]).split(':')[1].strip()

  def _GetRelPath(self, filename):
    """Get relative path of a file according to the current directory,
    given its logical path in the repo."""
    assert filename.startswith(self.subdir), (filename, self.subdir)
    return filename[len(self.subdir):].lstrip(r"\/")

  def GenerateDiff(self, extra_args):
    # If no file specified, restrict to the current subdir
    extra_args = extra_args or ["."]
    cmd = ["hg", "diff", "--git", "-r", self.base_rev] + extra_args
    data = run_shell(cmd, silent_ok=True)
    svndiff = []
    filecount = 0
    for line in data.splitlines():
      m = re.match("diff --git a/(\S+) b/(\S+)", line)
      if m:
        # Modify line to make it look like as it comes from svn diff.
        # With this modification no changes on the server side are required
        # to make upload.py work with Mercurial repos.
        # NOTE: for proper handling of moved/copied files, we have to use
        # the second filename.
        filename = m.group(2)
        svndiff.append("Index: %s" % filename)
        svndiff.append("=" * 67)
        filecount += 1
        logging.info(line)
      else:
        svndiff.append(line)
    if not filecount:
      ErrorExit("No valid patches found in output from hg diff")
    return "\n".join(svndiff) + "\n"

  def GetUnknownFiles(self):
    """Return a list of files unknown to the VCS."""
    args = []
    status = run_shell(["hg", "status", "--rev", self.base_rev, "-u", "."],
        silent_ok=True)
    unknown_files = []
    for line in status.splitlines():
      st, fn = line.split(" ", 1)
      if st == "?":
        unknown_files.append(fn)
    return unknown_files

  def GetBaseFile(self, filename):
    # "hg status" and "hg cat" both take a path relative to the current subdir
    # rather than to the repo root, but "hg diff" has given us the full path
    # to the repo root.
    base_content = ""
    new_content = None
    is_binary = False
    oldrelpath = relpath = self._GetRelPath(filename)
    # "hg status -C" returns two lines for moved/copied files, one otherwise
    out = run_shell(["hg", "status", "-C", "--rev", self.base_rev, relpath])
    out = out.splitlines()
    # HACK: strip error message about missing file/directory if it isn't in
    # the working copy
    if out[0].startswith('%s: ' % relpath):
      out = out[1:]
    if len(out) > 1:
      # Moved/copied => considered as modified, use old filename to
      # retrieve base contents
      oldrelpath = out[1].strip()
      status = "M"
    else:
      status, _ = out[0].split(' ', 1)
    if ":" in self.base_rev:
      base_rev = self.base_rev.split(":", 1)[0]
    else:
      base_rev = self.base_rev
    if status != "A":
      base_content = run_shell(["hg", "cat", "-r", base_rev, oldrelpath],
        silent_ok=True)
      is_binary = "\0" in base_content  # Mercurial's heuristic
    if status != "R":
      new_content = open(relpath, "rb").read()
      is_binary = is_binary or "\0" in new_content
    if is_binary and base_content:
      # Fetch again without converting newlines
      base_content = run_shell(["hg", "cat", "-r", base_rev, oldrelpath],
        silent_ok=True, universal_newlines=False)
    if not is_binary or not self.IsImage(relpath):
      new_content = None
    return base_content, new_content, is_binary, status


# NOTE: The SplitPatch function is duplicated in engine.py, keep them in sync.
def SplitPatch(data):
  """Splits a patch into separate pieces for each file.

  Args:
    data: A string containing the output of svn diff.

  Returns:
    A list of 2-tuple (filename, text) where text is the svn diff output
      pertaining to filename.
  """
  patches = []
  filename = None
  diff = []
  for line in data.splitlines(True):
    new_filename = None
    if line.startswith('Index:'):
      unused, new_filename = line.split(':', 1)
      new_filename = new_filename.strip()
    elif line.startswith('Property changes on:'):
      unused, temp_filename = line.split(':', 1)
      # When a file is modified, paths use '/' between directories, however
      # when a property is modified '\' is used on Windows.  Make them the same
      # otherwise the file shows up twice.
      temp_filename = temp_filename.strip().replace('\\', '/')
      if temp_filename != filename:
        # File has property changes but no modifications, create a new diff.
        new_filename = temp_filename
    if new_filename:
      if filename and diff:
        patches.append((filename, ''.join(diff)))
      filename = new_filename
      diff = [line]
      continue
    if diff is not None:
      diff.append(line)
  if filename and diff:
    patches.append((filename, ''.join(diff)))
  return patches


def UploadSeparatePatches(issue, rpc_server, patchset, data, options):
  """Uploads a separate patch for each file in the diff output.

  Returns a list of [patch_key, filename] for each file.
  """
  patches = SplitPatch(data)
  rv = []
  for patch in patches:
    if len(patch[1]) > MAX_UPLOAD_SIZE:
      print ("Not uploading the patch for " + patch[0] +
             " because the file is too large.")
      continue
    form_fields = [("filename", patch[0])]
    if not options.download_base:
      form_fields.append(("content_upload", "1"))
    files = [("data", "data.diff", patch[1])]
    ctype, body = encode_multipart_form_data(form_fields, files)
    url = "/%d/upload_patch/%d" % (int(issue), int(patchset))
    print "Uploading patch for " + patch[0]
    response_body = rpc_server.Send(url, body, content_type=ctype)
    lines = response_body.splitlines()
    if not lines or lines[0] != "OK":
      status_update("  --> %s" % response_body)
      sys.exit(1)
    rv.append([lines[1], patch[0]])
  return rv


def GuessVCSName():
  """Helper to guess the version control system.

  This examines the current directory, guesses which VersionControlSystem
  we're using, and returns an string indicating which VCS is detected.

  Returns:
    A pair (vcs, output).  vcs is a string indicating which VCS was detected
    and is one of VCS_GIT, VCS_MERCURIAL, VCS_SUBVERSION, or VCS_UNKNOWN.
    output is a string containing any interesting output from the vcs
    detection routine, or None if there is nothing interesting.
  """
  # Mercurial has a command to get the base directory of a repository
  # Try running it, but don't die if we don't have hg installed.
  # NOTE: we try Mercurial first as it can sit on top of an SVN working copy.
  try:
    out, returncode = run_shell_with_return_code(["hg", "root"])
    if returncode == 0:
      return (VCS_MERCURIAL, out.strip())
  except OSError, (errno, message):
    if errno != 2:  # ENOENT -- they don't have hg installed.
      raise

  # Subversion has a .svn in all working directories.
  if os.path.isdir('.svn'):
    logging.info("Guessed VCS = Subversion")
    return (VCS_SUBVERSION, None)

  # Git has a command to test if you're in a git tree.
  # Try running it, but don't die if we don't have git installed.
  try:
    out, returncode = run_shell_with_return_code(["git", "rev-parse",
                                              "--is-inside-work-tree"])
    if returncode == 0:
      return (VCS_GIT, None)
  except OSError, (errno, message):
    if errno != 2:  # ENOENT -- they don't have git installed.
      raise

  return (VCS_UNKNOWN, None)


def GuessVCS(options):
  """Helper to guess the version control system.

  This verifies any user-specified VersionControlSystem (by command line
  or environment variable).  If the user didn't specify one, this examines
  the current directory, guesses which VersionControlSystem we're using,
  and returns an instance of the appropriate class.  Exit with an error
  if we can't figure it out.

  Returns:
    A VersionControlSystem instance. Exits if the VCS can't be guessed.
  """
  vcs = options.vcs
  if not vcs:
    vcs = os.environ.get("CODEREVIEW_VCS")
  if vcs:
    v = VCS_ABBREVIATIONS.get(vcs.lower())
    if v is None:
      ErrorExit("Unknown version control system %r specified." % vcs)
    (vcs, extra_output) = (v, None)
  else:
    (vcs, extra_output) = GuessVCSName()

  if vcs == VCS_MERCURIAL:
    if extra_output is None:
      extra_output = run_shell(["hg", "root"]).strip()
    return MercurialVCS(options, extra_output)
  elif vcs == VCS_SUBVERSION:
    return SubversionVCS(options)
  elif vcs == VCS_GIT:
    return GitVCS(options)

  ErrorExit(("Could not guess version control system. "
             "Are you in a working copy directory?"))


def CheckReviewer(reviewer):
  """Validate a reviewer -- either a nickname or an email addres.

  Args:
    reviewer: A nickname or an email address.

  Calls ErrorExit() if it is an invalid email address.
  """
  if "@" not in reviewer:
    return  # Assume nickname
  parts = reviewer.split("@")
  if len(parts) > 2:
    ErrorExit("Invalid email address: %r" % reviewer)
  assert len(parts) == 2
  if "." not in parts[1]:
    ErrorExit("Invalid email address: %r" % reviewer)


def LoadSubversionAutoProperties():
  """Returns the content of [auto-props] section of Subversion's config file as
  a dictionary.

  Returns:
    A dictionary whose key-value pair corresponds the [auto-props] section's
      key-value pair.
    In following cases, returns empty dictionary:
      - config file doesn't exist, or
      - 'enable-auto-props' is not set to 'true-like-value' in [miscellany].
  """
  # Todo(hayato): Windows users might use different path for configuration file.
  subversion_config = os.path.expanduser("~/.subversion/config")
  if not os.path.exists(subversion_config):
    return {}
  config = ConfigParser.ConfigParser()
  config.read(subversion_config)
  if (config.has_section("miscellany") and
      config.has_option("miscellany", "enable-auto-props") and
      config.getboolean("miscellany", "enable-auto-props") and
      config.has_section("auto-props")):
    props = {}
    for file_pattern in config.options("auto-props"):
      props[file_pattern] = ParseSubversionPropertyValues(
        config.get("auto-props", file_pattern))
    return props
  else:
    return {}

def ParseSubversionPropertyValues(props):
  """Parse the given property value which comes from [auto-props] section and
  returns a list whose element is a (svn_prop_key, svn_prop_value) pair.

  See the following doctest for example.

  >>> ParseSubversionPropertyValues('svn:eol-style=LF')
  [('svn:eol-style', 'LF')]
  >>> ParseSubversionPropertyValues('svn:mime-type=image/jpeg')
  [('svn:mime-type', 'image/jpeg')]
  >>> ParseSubversionPropertyValues('svn:eol-style=LF;svn:executable')
  [('svn:eol-style', 'LF'), ('svn:executable', '*')]
  """
  key_value_pairs = []
  for prop in props.split(";"):
    key_value = prop.split("=")
    assert len(key_value) <= 2
    if len(key_value) == 1:
      # If value is not given, use '*' as a Subversion's convention.
      key_value_pairs.append((key_value[0], "*"))
    else:
      key_value_pairs.append((key_value[0], key_value[1]))
  return key_value_pairs


def GetSubversionPropertyChanges(filename):
  """Return a Subversion's 'Property changes on ...' string, which is used in
  the patch file.

  Args:
    filename: filename whose property might be set by [auto-props] config.

  Returns:
    A string like 'Property changes on |filename| ...' if given |filename|
      matches any entries in [auto-props] section. None, otherwise.
  """
  global svn_auto_props_map
  if svn_auto_props_map is None:
    svn_auto_props_map = LoadSubversionAutoProperties()

  all_props = []
  for file_pattern, props in svn_auto_props_map.items():
    if fnmatch.fnmatch(filename, file_pattern):
      all_props.extend(props)
  if all_props:
    return FormatSubversionPropertyChanges(filename, all_props)
  return None


def FormatSubversionPropertyChanges(filename, props):
  """Returns Subversion's 'Property changes on ...' strings using given filename
  and properties.

  Args:
    filename: filename
    props: A list whose element is a (svn_prop_key, svn_prop_value) pair.

  Returns:
    A string which can be used in the patch file for Subversion.

  See the following doctest for example.

  >>> print FormatSubversionPropertyChanges('foo.cc', [('svn:eol-style', 'LF')])
  Property changes on: foo.cc
  ___________________________________________________________________
  Added: svn:eol-style
     + LF
  <BLANKLINE>
  """
  prop_changes_lines = [
    "Property changes on: %s" % filename,
    "___________________________________________________________________"]
  for key, value in props:
    prop_changes_lines.append("Added: " + key)
    prop_changes_lines.append("   + " + value)
  return "\n".join(prop_changes_lines) + "\n"


def RealMain(argv, data=None):
  """The real main function.

  Args:
    argv: Command line arguments.
    data: Diff contents. If None (default) the diff is generated by
      the VersionControlSystem implementation returned by GuessVCS().

  Returns:
    A 2-tuple (issue id, patchset id).
    The patchset id is None if the base files are not uploaded by this
    script (applies only to SVN checkouts).
  """
  logging.basicConfig(format=("%(asctime).19s %(levelname)s %(filename)s:"
                              "%(lineno)s %(message)s "))
  os.environ['LC_ALL'] = 'C'
  options, args = parser.parse_args(argv[1:])
  global verbosity
  verbosity = options.verbose
  if verbosity >= 3:
    logging.getLogger().setLevel(logging.DEBUG)
  elif verbosity >= 2:
    logging.getLogger().setLevel(logging.INFO)

  vcs = GuessVCS(options)

  base = options.base_url
  if isinstance(vcs, SubversionVCS):
    # Guessing the base field is only supported for Subversion.
    # Note: Fetching base files may become deprecated in future releases.
    guessed_base = vcs.GuessBase(options.download_base)
    if base:
      if guessed_base and base != guessed_base:
        print "Using base URL \"%s\" from --base_url instead of \"%s\"" % \
            (base, guessed_base)
    else:
      base = guessed_base

  if not base and options.download_base:
    options.download_base = True
    logging.info("Enabled upload of base file")
  if not options.assume_yes:
    vcs.CheckForUnknownFiles()
  if data is None:
    data = vcs.GenerateDiff(args)
  files = vcs.GetBaseFiles(data)
  if verbosity >= 1:
    print "Upload server:", options.server, "(change with -s/--server)"
  if options.issue:
    prompt = "Message describing this patch set: "
  else:
    prompt = "New issue subject: "
  message = options.message or raw_input(prompt).strip()
  if not message:
    ErrorExit("A non-empty message is required")
  rpc_server = get_rpc_server(options)
  form_fields = [("subject", message)]
  if base:
    form_fields.append(("base", base))
  if options.issue:
    form_fields.append(("issue", str(options.issue)))
  if options.email:
    form_fields.append(("user", options.email))
  if options.reviewers:
    for reviewer in options.reviewers.split(','):
      CheckReviewer(reviewer)
    form_fields.append(("reviewers", options.reviewers))
  if options.cc:
    for cc in options.cc.split(','):
      CheckReviewer(cc)
    form_fields.append(("cc", options.cc))
  description = options.description
  if options.description_file:
    if options.description:
      ErrorExit("Can't specify description and description_file")
    file = open(options.description_file, 'r')
    description = file.read()
    file.close()
  if description:
    form_fields.append(("description", description))
  # Send a hash of all the base file so the server can determine if a copy
  # already exists in an earlier patchset.
  base_hashes = ""
  for file, info in files.iteritems():
    if not info[0] is None:
      checksum = md5(info[0]).hexdigest()
      if base_hashes:
        base_hashes += "|"
      base_hashes += checksum + ":" + file
  form_fields.append(("base_hashes", base_hashes))
  if options.private:
    if options.issue:
      print "Warning: Private flag ignored when updating an existing issue."
    else:
      form_fields.append(("private", "1"))
  # If we're uploading base files, don't send the email before the uploads, so
  # that it contains the file status.
  if options.send_mail and options.download_base:
    form_fields.append(("send_mail", "1"))
  if not options.download_base:
    form_fields.append(("content_upload", "1"))
  if len(data) > MAX_UPLOAD_SIZE:
    print "Patch is large, so uploading file patches separately."
    uploaded_diff_file = []
    form_fields.append(("separate_patches", "1"))
  else:
    uploaded_diff_file = [("data", "data.diff", data)]
  ctype, body = encode_multipart_form_data(form_fields, uploaded_diff_file)
  response_body = rpc_server.Send("/upload", body, content_type=ctype)
  patchset = None
  if not options.download_base or not uploaded_diff_file:
    lines = response_body.splitlines()
    if len(lines) >= 2:
      msg = lines[0]
      patchset = lines[1].strip()
      patches = [x.split(" ", 1) for x in lines[2:]]
    else:
      msg = response_body
  else:
    msg = response_body
  status_update(msg)
  if not response_body.startswith("Issue created.") and \
  not response_body.startswith("Issue updated."):
    sys.exit(0)
  issue = msg[msg.rfind("/")+1:]

  if not uploaded_diff_file:
    result = UploadSeparatePatches(issue, rpc_server, patchset, data, options)
    if not options.download_base:
      patches = result

  if not options.download_base:
    vcs.UploadBaseFiles(issue, rpc_server, patches, patchset, options, files)
    if options.send_mail:
      rpc_server.Send("/" + issue + "/mail", payload="")
  return issue, patchset


def main():
  try:
    RealMain(sys.argv)
  except KeyboardInterrupt:
    print
    status_update("Interrupted.")
    sys.exit(1)


if __name__ == "__main__":
  main()
