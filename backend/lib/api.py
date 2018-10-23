import threading
import socket
import time
import json

import config
from backend.lib.database import Database


class InternalAPI(threading.Thread):
	port = config.API_PORT
	looping = True
	manager = None
	db = None

	def __init__(self, manager, *args, **kwargs):
		"""
		Set up database connection

		:param manager:  Worker manager, of which this is a child thread
		:param args:
		:param kwargs:
		"""
		super().__init__(*args, **kwargs)
		self.manager = manager

		self.db = Database(logger=manager.log)

	def run(self):
		"""
		Listen for API requests

		Opens a socket that continuously listens for requests, and passes a
		client object on to a handling method if a connection is established
		
		:return:
		"""
		if self.port == 0:
			self.db.close()
			self.manager.log.info("Local API not available per configuration")
			return

		server = socket.socket()
		server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		server.settimeout(5)  # should be plenty

		has_time = True
		start_trying = int(time.time())
		while has_time:
			has_time = start_trying > time.time() - 300  # stop trying after 5 minutes
			try:
				server.bind(("localhost", self.port))
				break
			except OSError as e:
				if has_time and self.looping:
					self.manager.log.info("Could not open port %i yet (%s), retrying in 5 seconds" % (self.port, e))
					time.sleep(5.0)  # wait a few seconds before retrying
					continue
				self.manager.log.error(
					"Port %s is already in use and could not be released! Local API not available." % self.port)
				return
			except ConnectionRefusedError:
				self.manager.log.error("OS refused listening at port %i! Local API not available." % self.port)
				return

		server.listen(5)
		server.settimeout(5)
		self.manager.log.info("Local API listening for requests at localhost:%s" % self.port)

		# continually listen for new connections
		while self.looping:
			try:
				client, address = server.accept()
			except (socket.timeout, TimeoutError) as e:
				if not self.looping:
					break
				# no problemo, just listen again - this only times out so it won't hang the entire app when
				# trying to exit, as there's no other way to easily interrupt accept()
				continue

			self.api_response(client, address)
			client.close()

		self.manager.log.info("Shutting down local API server")

	def abort(self):
		"""
		Stop listening - tells the listening thread to unbind socket
		"""
		self.looping = False

	def api_response(self, client, address):
		"""
		Respond to API requests

		Gets the request, parses it as JSON, and if it is indeed JSON and of
		the proper format, a response is generated and returned as JSON via
		the client object

		:param client:  Client object representing the connection
		:param tuple address:  (IP, port) of the request
		:return: Response
		"""
		client.settimeout(2)  # should be plenty

		try:
			buffer = ""
			while True:
				# receive data in 1k chunks
				data = client.recv(1024).decode("ascii")
				buffer += data
				if not data or data.strip() == "" or len(data) > 2048:
					break

				# start processing as soon as we have valid json
				try:
					test = json.loads(buffer)
					break
				except json.JSONDecodeError:
					pass

			if not buffer:
				raise InternalAPIException
		except (socket.timeout, TimeoutError, ConnectionError, InternalAPIException):
			# this means that no valid request was sent
			self.manager.log.info("No input on API call from %s:%s - closing" % address)
			return False

		self.manager.log.debug("Received API request from %s:%s" % address)

		try:
			payload = json.loads(buffer)
			if "request" not in payload:
				raise InternalAPIException

			response = self.process_request(payload["request"], payload)
			if not response:
				raise InternalAPIException
		except (json.JSONDecodeError, InternalAPIException):
			return client.sendall(json.dumps({"error": "Invalid JSON"}).encode("ascii"))

		return client.sendall(json.dumps({"error": False, "response": response}).encode("ascii"))

	def process_request(self, request, payload):
		"""
		Generate API response

		Checks the type of request, and returns an appropriate response for
		the type.

		:param str request:  Request identifier
		:param payload:  Other data sent with the request
		:return:  API response
		"""
		if request == "workers":
			# return the number of workers, sorted by type
			workers = {}
			for worker in self.manager.pool:
				type = worker.__class__.__name__
				if type not in workers:
					workers[type] = 0
				workers[type] += 1

			workers["total"] = sum([workers[workertype] for workertype in workers])

			return workers

		if request == "posts" or request == "threads":
			# return the amount of posts or threads scraped in the past minute, day and hour
			now = int(time.time())
			then = now - 86400
			field = "timestamp" if request == "posts" else "timestamp_scraped"
			items = self.db.fetchall("SELECT * FROM " + request + " WHERE " + field + " > %s", (then,))
			if items is None:
				return {"error": "Database unavailable"}

			response = {
				"1m": 0,
				"1h": 0,
				"1d": 0
			}
			for item in items:
				response["1d"] += 1
				if item[field] > now - 60:
					response["1m"] += 1
				if item[field] > now - 3600:
					response["1h"] += 1
			return response

		if request == "jobs":
			# return queued jobs, sorted by type
			jobs = self.db.fetchall("SELECT * FROM jobs")
			if jobs is None:
				return {"error": "Database unavailable"}

			response = {}
			for job in jobs:
				if job["jobtype"] not in response:
					response[job["jobtype"]] = 0
				response[job["jobtype"]] += 1

			response["total"] = sum([response[jobtype] for jobtype in response])

			return response

		# no appropriate response
		return False


class InternalAPIException(Exception):
	# raised if API request could not be parsed
	pass