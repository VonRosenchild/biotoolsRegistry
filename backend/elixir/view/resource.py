from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticatedOrReadOnly, IsAuthenticated
from elixir.permissions import IsOwnerOrReadOnly, HasEditPermissionToEditResourceOrReadOnly, CanConcludeResourceRequest, IsStaffOrReadOnly
from rest_framework.settings import api_settings
import elixir.search as search
import elixir.logging as logging
from django.conf import settings
from rest_framework.response import Response
from elasticsearch import Elasticsearch
from elixir.models import *
from elixir.serializers import *
from django.http import Http404
import uuid
from django.db.models import Q
from elixir.renderers import XMLSchemaRenderer
from rest_framework.renderers import JSONRenderer
from rest_framework.renderers import BrowsableAPIRenderer
from rest_framework_yaml.renderers import YAMLRenderer

from rest_framework.parsers import JSONParser
from rest_framework_yaml.parsers import YAMLParser
from elixir.parsers import XMLSchemaParser

es = Elasticsearch(settings.ELASTIC_SEARCH_URLS)

class ResourceList(APIView):
	"""
	List all resources, or create a new resource.
	"""
	permission_classes = (IsAuthenticatedOrReadOnly,)
	parser_classes = (JSONParser, XMLSchemaParser, YAMLParser)
	renderer_classes = (BrowsableAPIRenderer, JSONRenderer, XMLSchemaRenderer, YAMLRenderer)
	
	def get(self, request, format=None):
		query_dict = request.GET
		size = api_settings.PAGE_SIZE
		page = int(query_dict.get('page', '1'))

		searchLogger = logging.SearchLogger(query_dict)
		searchLogger.commit()

		domain = query_dict.get('domain', None)
		domain_resources = []
		query_struct = search.construct_es_query(query_dict)

		#return Response({str(query_struct)}, status=status.HTTP_200_OK)

		result = es.search(index=settings.ELASTIC_SEARCH_INDEX, body=query_struct)
		count = result['hits']['total']
		results = [el['_source'] for el in result['hits']['hits']]

		# check if page is valid
		if (not results and count > 0):
			return Response({"detail": "Invalid page. That page contains no results."}, status=status.HTTP_404_NOT_FOUND)

		if domain:
			domain_result = es.search(index='domains', body={'size': 10000,'query': {'bool': {'must': [{'match_phrase': {'domain': {'query': domain}}}]}}})
			domain_count = domain_result['hits']['total']
			
			if domain_count > 0:
				domain_result = [el['_source'] for el in domain_result['hits']['hits']][0]
				domain_resources = set(map(lambda x: (x['biotoolsID']), domain_result['resources']))

				# get touples of returned tools
				returned_resource = set(map(lambda x: (x['biotoolsID']), results))
				
				
				
				if len(list(set(query_dict.keys()) - set([u'sort', u'domain', u'ord', u'page']))) == 0:
					diff = list(domain_resources)
				else:
					diff = list(returned_resource & domain_resources)
				
				if len(diff) > 0:
					count = len(diff)
					
					if len(diff) > 1000:
						results = []
						for i in range(0,len(diff) / 1000):
							rest = len(diff) if len(diff) <= i*1000+1000 else i*1000+1000
							query_struct['query'] = {'bool': {'should': map(lambda x: {'bool': {'must': [{'match': {'id': {'query': x[0]}}}]}}, diff[i*1000:rest])}}
							result = es.search(index='elixir', body=query_struct)
							sub_results = [el['_source'] for el in result['hits']['hits']]
							results += sub_results
					else:

						query_struct['query'] = {'bool': {'should': map(lambda x: {'bool': {'must': [{'match': {'biotoolsID': {'query': x}}}]}}, diff)}}


						result = es.search(index='elixir', body=query_struct)
						#return Response({str(result)}, status=status.HTTP_400_BAD_REQUEST)
						count = result['hits']['total']
						results = [el['_source'] for el in result['hits']['hits']]
				else:
					return Response({'count': 0,
						 'next': None if (page*size >= count) else "?page=" + str(page + 1),
						 'previous': None if page == 1 else "?page=" + str(page - 1),
						 'list': []}, status=200)
			else:
				return Response({'count': 0,
					'next': None if (page*size >= count) else "?page=" + str(page + 1),
				 	'previous': None if page == 1 else "?page=" + str(page - 1),
				 	'list': []}, status=200)

		return Response({'count': count,
						 'next': None if (page*size >= count) else "?page=" + str(page + 1),
						 'previous': None if page == 1 else "?page=" + str(page - 1),
						 'list': results}, status=200)

	def post(self, request, format=None):
		serializer = ResourceSerializer(data=request.data, context={'request':request,"request_type":"POST"})

		if serializer.is_valid():
			serializer.save(owner=request.user)
			issue_function(Resource.objects.get(biotoolsID=serializer.data['biotoolsID'], visibility=1), request.user)

			es.index(index=settings.ELASTIC_SEARCH_INDEX, doc_type='tool', body=serializer.data)
			return Response(serializer.data, status=status.HTTP_201_CREATED)
		return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
		

class DisownResourceView(APIView):
	"""
	Disown the request
	"""
	permission_classes = (IsAuthenticatedOrReadOnly, IsOwnerOrReadOnly, )

	def get_object(self, biotoolsID):
		try:
			obj = Resource.objects.filter(visibility=1).get(biotoolsID__iexact=biotoolsID)
			self.check_object_permissions(self.request, obj)
			return obj
		except Resource.DoesNotExist:
			raise Http404

	def get_disowned_user(self):
		return User.objects.get(username__iexact="admin")
 
	def post(self, request, biotoolsID, format=None):
		resource = self.get_object(biotoolsID)
		resource.owner = self.get_disowned_user()
		resource.save()
		return Response({"detail": "You have successfully disowned your entry."}, status=status.HTTP_200_OK)


class ResourceRequestView(APIView):
	"""
	Create or obtain resource requests.
	"""
	permission_classes = (IsAuthenticatedOrReadOnly,)

	def put(self, request, format=None):
		serializerData = request.data
		serializerData['username'] = request.user.username
		serializerData['requestId'] = uuid.uuid4()
		serializer = ResourceRequestSerializer(data=serializerData)
		if serializer.is_valid():
			serializer.save()
			return Response(serializer.data, status=status.HTTP_201_CREATED)
		return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

	def get(self, request, format=None):
		my_requests = ResourceRequest.objects.filter(user=request.user)
		if request.user.is_superuser == True:
			received_requests = ResourceRequest.objects.all()
		else:
			received_requests = ResourceRequest.objects.filter(Q(resource__owner=request.user) | Q(completedBy=request.user))
		received_requests_serializer = ResourceRequestSerializer(instance=received_requests, many=True)
		my_requests_serializer = ResourceRequestSerializer(instance=my_requests, many=True)
		return Response({'requests': {'received': received_requests_serializer.data, 'sent': my_requests_serializer.data}}, status=status.HTTP_200_OK)


class ProcessResourceRequest(APIView):
	"""
	Retrieve, update or delete a resource instance.
	"""
	permission_classes = (IsAuthenticatedOrReadOnly, CanConcludeResourceRequest)

	def post(self, request, format=None):
		if not 'requestId' in request.data: 
			return Response({"detail": "Request could not be completed. Missing parameter: 'requestId'."}, status=status.HTTP_400_BAD_REQUEST)
		if not 'accept' in request.data: 
			return Response({"detail": "Request could not be completed. Missing parameter: 'accept'."}, status=status.HTTP_400_BAD_REQUEST)
		try:
			resourceRequest = ResourceRequest.objects.get(requestId=request.data['requestId'])
		except:
   			return Response({"detail": "No active requests with a specified 'requestId' could be found."}, status=status.HTTP_404_NOT_FOUND)
   		self.check_object_permissions(self.request, resourceRequest)
   		if resourceRequest.completed == True: 
			return Response({"detail": "Request has already been concluded."}, status=status.HTTP_400_BAD_REQUEST)
   		serializer = ResourceRequestConcludeSerializer(resourceRequest, data=request.data, context={'user': request.user})
   		if serializer.is_valid():
			serializer.save()
		responseSerializer = ResourceRequestSerializer(resourceRequest)
		return Response(responseSerializer.data, status=status.HTTP_200_OK)


class ResourceDetail(APIView):
	"""
	Retrieve a specific resource
	"""
	permission_classes = (IsAuthenticatedOrReadOnly, HasEditPermissionToEditResourceOrReadOnly)
	#if interested in the resource update response content (other than the status)
	# then should add ?format=json or format=xml or format=yaml
	# because otherwise it will be by default format=api
	# to really make it clean, should have renderer for get() and rederers for put and delete
	# best here is to use functions instead of classes, and have api_view decorators for functions
	renderer_classes = (BrowsableAPIRenderer, JSONRenderer, XMLSchemaRenderer, YAMLRenderer)
	parser_classes = (JSONParser, XMLSchemaParser, YAMLParser)

	def get_object(self, biotoolsID):
		try:
			obj = Resource.objects.filter(visibility=1).get(biotoolsID__iexact=biotoolsID)
			self.check_object_permissions(self.request, obj)
			return obj
		except Resource.DoesNotExist:
			raise Http404

	def check_for_edit_permissions(self, request, resource):
		if request.user == resource.owner or request.user.is_superuser:
			return True
		return False

	def check_editing_permissions(self, request, resource):
		if 'editPermission' in request.data:
			if 'type' in request.data['editPermission'] and resource.editPermission.type != request.data['editPermission']['type']:
				return True
			authors = set(resource.editPermission.authors.all().values_list('user__username', flat=True))
			if not 'authors' in request.data['editPermission']:	
				return False
			authorsRequest = set(request.data['editPermission']['authors'])
			if authors != authorsRequest:
				return True
		return False

	def get(self, request, biotoolsID, format=None):
		resource = self.get_object(biotoolsID)
		serializer = ResourceSerializer(resource)
		return Response(serializer.data)


	def process_request_for_otherID(self, request, rID):
		# only works if not superuser, and otherID is present and a valid list
		if not(request.user.is_superuser) and request.data.get('otherID') and isinstance(request.data['otherID'],list):
			for oID in list(request.data['otherID']):
				# we check for the "value" property on otherID
				if oID.get("value") and oID["value"].lower().startswith("biotools:"): 
					#remove if value is for "biotools" otherID
					request.data['otherID'].remove(oID)


		# retrieve otherID of type biotools, if any, from current version of tool
		# and add them to the request object
		o_objects = OtherID.objects.filter(resource_id=rID,value__startswith='biotools:')
		if len(o_objects) > 0:
			if not(request.user.is_superuser) and (not(request.data.get('otherID')) or not(isinstance(request.data['otherID'],list))):
				request.data['otherID'] = []

			for o in o_objects:
				o_new = {}
				if o.value:
					o_new['value'] = o.value
				if o.type:
					o_new['type'] = o.type
				if o.version:
					o_new['version'] = o.version
				request.data['otherID'].append(o_new)

			# if we removed all values and otherID is empty we need to remove it 
			# to make sure we don't get validation error
		if request.data.get('otherID') and isinstance(request.data['otherID'],list) and len(request.data['otherID']) == 0:
			del request.data['otherID']

		return request

	# the update is actually creating a brand new resource, copying a few key information, and setting the visibility of the original to 0
	#use ?format=json or ?format=xml  if doing update (PUT) in the API
	# default is ?format=api
	def put(self, request, biotoolsID, format=None):
		resource = self.get_object(biotoolsID)
		canEditPermissions = self.check_for_edit_permissions(request, resource)
		isEditingPermissions = self.check_editing_permissions(request, resource)
		if canEditPermissions == False and isEditingPermissions == True:
			return Response({"detail": "Only the owner can edit permissions for a specified resource."}, status=status.HTTP_401_UNAUTHORIZED)
		# Copy permissions from the esisting resource in case not specified.
		if isEditingPermissions == False:
			permissionSerializer = EditPermissionSerializer(resource.editPermission)
			request.data['editPermission'] = permissionSerializer.data

		request = self.process_request_for_otherID(request, resource.id)

		serializer = ResourceUpdateSerializer(data=request.data,context={'request':request,"request_type":"PUT"})

		if serializer.is_valid():
			# setting the visibility of the current resource to 0
			resource.visibility = 0
			resource.save()
			# copying the textual id and additionDate to the newly created resource
			serializer.save(biotoolsID=resource.biotoolsID, biotoolsCURIE=resource.biotoolsCURIE, additionDate=resource.additionDate, owner=resource.owner)
			issue_function(Resource.objects.get(biotoolsID=serializer.data['biotoolsID'], visibility=1), str(resource.owner))
			
			# update the existing resource in elastic
			result = es.search(index=settings.ELASTIC_SEARCH_INDEX, body={
				"query": {
					"bool" : {
						"must": [
							{
								"match": {
									"biotoolsID": resource.biotoolsID.lower()
								}
							}
						]
					}
				}
			})
			count = result['hits']['total']
			if count == 1:
				es.index(index=settings.ELASTIC_SEARCH_INDEX, doc_type='tool', body=serializer.data, id=result['hits']['hits'][0]['_id'])
			return Response(serializer.data)
		
		return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

	def delete(self, request, biotoolsID, format=None):
		if request.user.is_superuser:
			resource = self.get_object(biotoolsID)
			# setting the visibility of the current resource to 0
			resource.visibility = 0
			resource.save()

			result = es.search(index=settings.ELASTIC_SEARCH_INDEX, body={
				"query": {
					"bool" : {
						"must": [
							{
								"match": {
									"biotoolsID": resource.biotoolsID.lower()
								}
							}
						]
					}
				}
			})
			count = result['hits']['total']
			if count == 1:
				es.delete(index=settings.ELASTIC_SEARCH_INDEX, doc_type='tool', id=result['hits']['hits'][0]['_id'])

			return Response(status=status.HTTP_204_NO_CONTENT)
		else:
			return Response({"detail": "Only a superuser can remove a resource."}, status=status.HTTP_403_FORBIDDEN)

# class ResourceDetailVersionList(APIView):
# 	"""
# 	Retrieve a list of versions for a resource
# 	"""
# 	# permission_classes = (IsAuthenticatedOrReadOnly,IsOwnerOrReadOnly)

# 	def get_list(self, biotoolsID):
# 		try:
# 			obj = Resource.objects.filter(visibility=1, biotoolsID__iexact=biotoolsID)
# 			self.check_object_permissions(self.request, obj)
# 			return obj
# 		except Resource.DoesNotExist:
# 			raise Http404

# 	def get(self, request, biotoolsID, version=None, format=None):
# 		resource = self.get_list(biotoolsID)
# 		serializer = VersionLatestSerializer(instance=resource, many=True)
# 		return Response(serializer.data)

class ResourceCreateValidator(APIView):
	"""
	Validate creating a resource.
	"""
	permission_classes = (IsAuthenticatedOrReadOnly,)
	parser_classes = (JSONParser, XMLSchemaParser, YAMLParser)
	renderer_classes = (BrowsableAPIRenderer, JSONRenderer, XMLSchemaRenderer, YAMLRenderer)
	#should also have renderers except the browsableapi one since we don't really need it...

	def post(self, request, format=None):
		# original
		#serializer = ResourceSerializer(data=request.data)

		# with context
		serializer = ResourceSerializer(data=request.data, context={'request':request,"request_type":"POST"})
		if serializer.is_valid():
			return Response(serializer.validated_data, status=status.HTTP_200_OK)
		return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class ResourceUpdateValidator(APIView):
	"""
	Validate updating a resource.
	"""
	permission_classes = (IsAuthenticatedOrReadOnly, HasEditPermissionToEditResourceOrReadOnly,)
	parser_classes = (JSONParser, XMLSchemaParser, YAMLParser)
	renderer_classes = (BrowsableAPIRenderer, JSONRenderer, XMLSchemaRenderer, YAMLRenderer)
	#should also have renderers except the browsableapi one since we don't really need it...

	def get_object(self, biotoolsID):
		try:
			obj = Resource.objects.filter(visibility=1).get(biotoolsID__iexact=biotoolsID)
			self.check_object_permissions(self.request, obj)
			return obj
		except Resource.DoesNotExist:
			raise Http404

	def put(self, request, biotoolsID, format=None):
		resource = self.get_object(biotoolsID)
		serializer = ResourceUpdateSerializer(data=request.data, context={'request':request,"request_type":"PUT"})
		if serializer.is_valid():
			return Response(serializer.validated_data)
		return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
		
