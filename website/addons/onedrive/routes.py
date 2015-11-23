# -*- coding: utf-8 -*-
"""Routes for the onedrive addon.
"""

from framework.routing import Rule, json_renderer

from . import views

# JSON endpoints
api_routes = {
    'rules': [

        #### Profile settings ###

        Rule(
            [
                '/settings/onedrive/accounts/',
            ],
            'get',
            views.config.list_onedrive_user_accounts,
            json_renderer,

        ),

        ##### Node settings #####

        Rule(
            ['/project/<pid>/onedrive/folders/',
             '/project/<pid>/node/<nid>/onedrive/folders/'],
            'get',
            views.hgrid.onedrive_folder_list,
            json_renderer
        ),

        Rule(
            ['/project/<pid>/onedrive/config/',
             '/project/<pid>/node/<nid>/onedrive/config/'],
            'get',
            views.config.onedrive_config_get,
            json_renderer
        ),

        Rule(
            ['/project/<pid>/onedrive/config/',
             '/project/<pid>/node/<nid>/onedrive/config/'],
            'put',
            views.config.onedrive_config_put,
            json_renderer
        ),

        Rule(
            ['/project/<pid>/onedrive/config/',
             '/project/<pid>/node/<nid>/onedrive/config/'],
            'delete',
            views.config.onedrive_remove_user_auth,
            json_renderer
        ),

        Rule(
            ['/project/<pid>/onedrive/config/import-auth/',
             '/project/<pid>/node/<nid>/onedrive/config/import-auth/'],
            'put',
            views.config.onedrive_import_user_auth,
            json_renderer
        ),
    ],
    'prefix': '/api/v1'
}
