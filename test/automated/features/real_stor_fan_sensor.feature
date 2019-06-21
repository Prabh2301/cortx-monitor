
Feature: Test FAN Sensor Capabilities
	Send FAN sensor request messages to SSPL and
	verify the response messages contain the correct information.

Scenario: Send SSPL a fan sensor message requesting fan data
	Given that SSPL is running
	When I send in the fan sensor message to request the current "enclosure_fan_alert" data
	Then I get the "enclosure_fan_alert" JSON response message
