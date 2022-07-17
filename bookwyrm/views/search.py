""" search views"""
import re

from django.contrib.postgres.search import TrigramSimilarity
from django.core.paginator import Paginator
from django.db.models.functions import Greatest
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.views import View

from bookwyrm import models
from bookwyrm.connectors import connector_manager
from bookwyrm.book_search import search, format_search_result
from bookwyrm.settings import PAGE_LENGTH
from bookwyrm.utils import regex
from .helpers import is_api_request
from .helpers import handle_remote_webfinger


# pylint: disable= no-self-use
class Search(View):
    """search users or books"""

    def get(self, request):
        """that search bar up top"""
        query = request.GET.get("q")
        # check if query is isbn
        query = isbn_check(query)
        min_confidence = request.GET.get("min_confidence", 0)
        search_type = request.GET.get("type")
        search_remote = (
            request.GET.get("remote", False) and request.user.is_authenticated
        )

        if is_api_request(request):
            # only return local book results via json so we don't cascade
            book_results = search(query, min_confidence=min_confidence)
            return JsonResponse(
                [format_search_result(r) for r in book_results], safe=False
            )

        if query and not search_type:
            search_type = "user" if "@" in query else "book"

        endpoints = {
            "book": book_search,
            "user": user_search,
            "list": list_search,
        }
        if not search_type in endpoints:
            search_type = "book"

        data = {
            "query": query or "",
            "type": search_type,
            "remote": search_remote,
        }
        if query:
            results, search_remote = endpoints[search_type](
                query, request.user, min_confidence, search_remote
            )
            if results:
                paginated = Paginator(results, PAGE_LENGTH).get_page(
                    request.GET.get("page")
                )
                data["results"] = paginated
                data["remote"] = search_remote

        return TemplateResponse(request, f"search/{search_type}.html", data)


def book_search(query, user, min_confidence, search_remote=False):
    """the real business is elsewhere"""
    # try a local-only search
    results = [{"results": search(query, min_confidence=min_confidence)}]
    if not user.is_authenticated or (results[0]["results"] and not search_remote):
        return results, False

    # if there were no local results, or the request was for remote, search all sources
    results += connector_manager.search(query, min_confidence=min_confidence)
    return results, True


def user_search(query, viewer, *_):
    """cool kids members only user search"""
    # logged out viewers can't search users
    if not viewer.is_authenticated:
        return models.User.objects.none(), None

    # use webfinger for mastodon style account@domain.com username to load the user if
    # they don't exist locally (handle_remote_webfinger will check the db)
    if re.match(regex.FULL_USERNAME, query):
        handle_remote_webfinger(query)

    return (
        models.User.viewer_aware_objects(viewer)
        .annotate(
            similarity=Greatest(
                TrigramSimilarity("username", query),
                TrigramSimilarity("localname", query),
            )
        )
        .filter(
            similarity__gt=0.5,
        )
        .order_by("-similarity")
    ), None


def list_search(query, viewer, *_):
    """any relevent lists?"""
    return (
        models.List.privacy_filter(
            viewer,
            privacy_levels=["public", "followers"],
        )
        .annotate(
            similarity=Greatest(
                TrigramSimilarity("name", query),
                TrigramSimilarity("description", query),
            )
        )
        .filter(
            similarity__gt=0.1,
        )
        .order_by("-similarity")
    ), None


def isbn_check(query):
    """isbn10 or isbn13 check, if so remove separators"""
    if query:
        su_num = re.sub(r"(?<=\d)\D(?=\d|[xX])", "", query)
        if len(su_num) == 13 and su_num.isdecimal():
            # Multiply every other digit by  3
            # Add these numbers and the other digits
            product = sum(int(ch) for ch in su_num[::2]) + sum(
                int(ch) * 3 for ch in su_num[1::2]
            )
            if product % 10 == 0:
                return su_num
        elif (
            len(su_num) == 10
            and su_num[:-1].isdecimal()
            and (su_num[-1].isdecimal() or su_num[-1].lower() == "x")
        ):
            product = 0
            # Iterate through code_string
            for i in range(9):
                # for each character, multiply by a different decreasing number: 10 - x
                product = product + int(su_num[i]) * (10 - i)
            # Handle last character
            if su_num[9].lower() == "x":
                product += 10
            else:
                product += int(su_num[9])
            if product % 11 == 0:
                return su_num
    return query
