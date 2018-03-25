"""
Copyright (c) 2018 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from atomic_reactor.plugins.pre_reactor_config import NO_FALLBACK, get_value


class KojiPluginMixin(object):

    @property
    def koji_config(self):
        if not hasattr(self, '__koji_config'):
            fallback = NO_FALLBACK
            if hasattr(self, 'koji_config_fallback'):
                fallback = self.koji_config_fallback
            self._koji_config = get_value(self.workflow, 'koji', fallback)

            if 'auth' in self._koji_config:
                krb_principal = self._koji_config['auth'].get('krb_principal')
                krb_keytab = self._koji_config['auth'].get('krb_keytab_path')
                if bool(krb_principal) != bool(krb_keytab):
                    raise RuntimeError("specify both koji_principal and koji_keytab or neither")

        return self._koji_config

    @property
    def koji_session(self):
        if not hasattr(self, '__koji_session'):
            # Import delayed until needed since koji lib is optional
            from atomic_reactor.koji_util import create_koji_session

            auth_info = {
                "proxyuser": self.koji_config['auth'].get('proxyuser'),
                "ssl_certs_dir": self.koji_config['auth'].get('ssl_certs_dir'),
                "krb_principal": self.koji_config['auth'].get('krb_principal'),
                "krb_keytab": self.koji_config['auth'].get('krb_keytab_path')
            }

            self.__koji_session = create_koji_session(self.koji_config['hub_url'], auth_info)

        return self.__koji_session

    @property
    def koji_pathinfo(self):
        if not hasattr(self, '__koji_pathinfo'):
            # Import delayed until needed since koji lib is optional
            import koji
            self.__koji_pathinfo = koji.PathInfo(topdir=self.koji_config['root_url'])

        return self.__koji_pathinfo
