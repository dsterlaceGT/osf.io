from website.addons.base.exceptions import AddonError


class OneDriveError(AddonError):
    pass


class OneDriveAuthError(OneDriveError):
    pass
