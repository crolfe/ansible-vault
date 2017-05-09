import os
# compatibility with Python >= 2.7.13
try:
    import urllib2
except ImportError:
    import urllib.request as urllib2

import json
import errno
import ssl
from distutils.version import StrictVersion
from sys import version_info

# compatibility with Python >= 2.7.13
try:
    from urlparse import urljoin
except ImportError:
    from urllib.parse import urljoin

from ansible.errors import AnsibleError
import ansible.utils
try:
    from ansible.plugins.lookup import LookupBase
except ImportError:
    # ansible-1.9.x
    class LookupBase(object):
        def __init__(self, basedir=None, runner=None, **kwargs):
            self.runner = runner
            self.basedir = basedir or (self.runner.basedir
                                       if self.runner
                                       else None)

        def get_basedir(self, variables):
            return self.basedir

_use_vault_cache = os.environ.get("ANSIBLE_HASHICORP_VAULT_USE_CACHE", "yes").lower() in ("yes", "1", "true")
_vault_cache = {}


class LookupModule(LookupBase):

    def run(self, terms, inject=None, variables=None, **kwargs):
        basedir = self.get_basedir(variables)

        if hasattr(ansible.utils, 'listify_lookup_plugin_terms'):
            # ansible-1.9.x
            terms = ansible.utils.listify_lookup_plugin_terms(terms, basedir, inject)

        term_split = terms[0].split(' ', 1)
        key = term_split[0]

        python_version_cur = ".".join([str(version_info.major),
                                       str(version_info.minor),
                                       str(version_info.micro)])
        python_version_min = "2.7.9"
        if StrictVersion(python_version_cur) < StrictVersion(python_version_min):
            raise AnsibleError('Unable to read %s from vault:'
                               ' Using Python %s, and vault lookup plugin requires at least %s'
                               ' to use an SSL context (VAULT_CACERT or VAULT_CAPATH)'
                               % (key, python_version_cur, python_version_min))

        try:
            parameters = term_split[1]
            parameters = parameters.split(' ')

            parameter_bag = {}
            for parameter in parameters:
                parameter_split = parameter.split('=')

                parameter_key = parameter_split[0]
                parameter_value = parameter_split[1]
                parameter_bag[parameter_key] = parameter_value

            data = json.dumps(parameter_bag)
        except Exception:
            data = None

        try:
            field = terms[1]
        except IndexError:
            field = None

        # the environment variable takes precendence over the Ansible variable.
        # Ansible variables are passed via "variables" in ansible 2.x, "inject" in 1.9.x
        url = os.getenv('VAULT_ADDR') or (variables or inject).get('vault_addr')
        if not url:
            raise AnsibleError('Vault address not set. Specify with'
                               ' VAULT_ADDR environment variable or vault_addr Ansible variable')

        # the environment variable takes precedence over the file-based token.
        # intentionally do *not* support setting this via an Ansible variable,
        # so as not to encourage bad security practices.
        github_token = os.getenv('VAULT_GITHUB_API_TOKEN')
        vault_token = os.getenv('VAULT_TOKEN')
        if not vault_token and not github_token:
            token_path = os.path.join(os.getenv('HOME'), '.vault-token')
            try:
                with open(token_path) as token_file:
                    vault_token = token_file.read().strip()
            except IOError as err:
                if err.errno != errno.ENOENT:
                    raise AnsibleError('Error occurred when opening ' + token_path + ': ' + err.strerror)
        if not github_token and not vault_token:
            raise AnsibleError('Vault or GitHub authentication token missing. Specify with'
                               ' VAULT_TOKEN/VAULT_GITHUB_API_TOKEN environment variable or in $HOME/.vault-token '
                               '(Current $HOME value is ' + os.getenv('HOME') + ')')

        cafile = os.getenv('VAULT_CACERT') or (variables or inject).get('vault_cacert')
        capath = os.getenv('VAULT_CAPATH') or (variables or inject).get('vault_capath')
        cahostverify = os.getenv('VAULT_CAHOSTVERIFY') or (variables or inject).get('vault_cahostverify') or 'yes'

        if _use_vault_cache and key in _vault_cache:
            result = _vault_cache[key]
        else:
            if not vault_token:
                token_result = self._fetch_token(cafile, capath, github_token, url, cahostverify)
                vault_token = token_result['auth']['client_token']
            result = self._fetch_remotely(cafile, capath, data, key, vault_token, url, cahostverify)
            if _use_vault_cache:
                _vault_cache[key] = result

        return [result['data'][field]] if field is not None else [result['data']]

    def _fetch_token(self, cafile, capath, github_token, url, cahostverify):
        try:
            context = None
            if cafile or capath:
                context = ssl.create_default_context(cafile=cafile, capath=capath)
                if cahostverify == 'no':
                    context.check_hostname = False
                else:
                    context.check_hostname = True
            request_url = urljoin(url, "v1/auth/github/login")
            req_params = {}
            req_params['token'] = github_token
            req = urllib2.Request(request_url, json.dumps(req_params))
            req.add_header('Content-Type', 'application/json')
            response = urllib2.urlopen(req, context=context) if context else urllib2.urlopen(req)
        except AttributeError as e:
            raise AnsibleError('Unable to retrieve personal token from vault: %s' % (e))
        except urllib2.HTTPError as e:
            raise AnsibleError('Unable to retrieve personal token from vault: %s' % (e))
        except Exception as e:
            raise AnsibleError('Unable to retrieve personal token from vault: %s' % (e))
        result = json.loads(response.read())
        return result

    def _fetch_remotely(self, cafile, capath, data, key, vault_token, url, cahostverify):
        try:
            context = None
            if cafile or capath:
                context = ssl.create_default_context(cafile=cafile, capath=capath)
                if cahostverify == 'no':
                    context.check_hostname = False
                else:
                    context.check_hostname = True
            request_url = urljoin(url, "v1/%s" % (key))
            req = urllib2.Request(request_url, data)
            req.add_header('X-Vault-Token', vault_token)
            req.add_header('Content-Type', 'application/json')
            response = urllib2.urlopen(req, context=context) if context else urllib2.urlopen(req)
        except AttributeError as e:
            raise AnsibleError('Unable to read %s from vault: %s' % (key, e))
        except urllib2.HTTPError as e:
            raise AnsibleError('Unable to read %s from vault: %s' % (key, e))
        except Exception as e:
            raise AnsibleError('Unable to read %s from vault: %s' % (key, e))
        result = json.loads(response.read())
        return result
