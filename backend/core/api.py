from collections.abc import Mapping

from rest_framework import status
from rest_framework.response import Response


def object_body_error(request):
    """Return a 400 response if the request body isn't a JSON object, else None.

    Views that read fields with request.data.get() / .keys() would otherwise
    crash with a 500 on a body such as [] or "x".
    """
    if isinstance(request.data, Mapping):
        return None
    return Response(
        {'detail': 'Request body must be a JSON object.'},
        status=status.HTTP_400_BAD_REQUEST,
    )
