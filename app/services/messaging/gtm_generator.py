"""
GTM Generator Service
Generates GTM container JSON for import.
"""
from datetime import datetime
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
import uuid

from app.models.messaging import (
    MessagingGTMContainer, MessagingEventSchema, MessagingDestination,
    MessagingEventMapping, MessagingDomain, DestinationType
)
from app.services.messaging.destination_config import destination_config


class GTMGenerator:
    """
    Generates Google Tag Manager container JSON for import.

    The generated container includes:
    - Versya SDK tag
    - Variables for event properties
    - Triggers for each event
    - Tags for each destination
    """

    def __init__(self):
        self._tag_id_counter = 1
        self._trigger_id_counter = 1
        self._variable_id_counter = 1

    def _reset_counters(self):
        """Reset ID counters for new generation."""
        self._tag_id_counter = 1
        self._trigger_id_counter = 1
        self._variable_id_counter = 1

    def _next_tag_id(self) -> str:
        id_val = self._tag_id_counter
        self._tag_id_counter += 1
        return str(id_val)

    def _next_trigger_id(self) -> str:
        id_val = self._trigger_id_counter
        self._trigger_id_counter += 1
        return str(id_val)

    def _next_variable_id(self) -> str:
        id_val = self._variable_id_counter
        self._variable_id_counter += 1
        return str(id_val)

    async def generate_gtm_container(
        self,
        db: Session,
        project_id: int,
        container_id: int
    ) -> Dict[str, Any]:
        """
        Generate complete GTM container JSON for a project.
        """
        self._reset_counters()

        # Get container config
        container = db.query(MessagingGTMContainer).filter(
            MessagingGTMContainer.id == container_id,
            MessagingGTMContainer.project_id == project_id
        ).first()

        if not container:
            raise ValueError("GTM container not found")

        # Get domain for write key
        domain = None
        if container.domain_id:
            domain = db.query(MessagingDomain).filter(
                MessagingDomain.id == container.domain_id
            ).first()

        # Get event schemas
        event_schemas = db.query(MessagingEventSchema).filter(
            MessagingEventSchema.project_id == project_id,
            MessagingEventSchema.is_active == True
        ).all()

        # Get destinations
        destinations = db.query(MessagingDestination).filter(
            MessagingDestination.project_id == project_id,
            MessagingDestination.is_active == True
        ).all()

        # Get event mappings
        mappings = db.query(MessagingEventMapping).filter(
            MessagingEventMapping.project_id == project_id,
            MessagingEventMapping.is_active == True
        ).all()

        # Generate container structure
        gtm_container = self._generate_container_structure(
            container_name=container.container_name,
            gtm_container_id=container.gtm_container_id
        )

        # Generate Versya SDK tag
        write_key = domain.write_key if domain else "YOUR_WRITE_KEY"
        versya_tag = self._generate_versya_sdk_tag(write_key)
        gtm_container["containerVersion"]["tag"].append(versya_tag)

        # Generate variables for common event properties
        variables = self._generate_variables(event_schemas)
        gtm_container["containerVersion"]["variable"].extend(variables)

        # Generate triggers for events
        triggers = self._generate_triggers(event_schemas)
        gtm_container["containerVersion"]["trigger"].extend(triggers)

        # Generate tags for destinations
        for dest in destinations:
            dest_mappings = [m for m in mappings if m.destination_id == dest.id]
            dest_tags = self._generate_destination_tags(dest, dest_mappings)
            gtm_container["containerVersion"]["tag"].extend(dest_tags)

        # Update container record
        container.generated_json = gtm_container
        container.last_generated_at = datetime.utcnow()
        container.version += 1
        db.commit()

        return gtm_container

    def _generate_container_structure(
        self,
        container_name: str,
        gtm_container_id: Optional[str]
    ) -> Dict[str, Any]:
        """Generate base GTM container structure."""
        return {
            "exportFormatVersion": 2,
            "exportTime": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "containerVersion": {
                "path": f"accounts/0/containers/0/versions/0",
                "accountId": "0",
                "containerId": "0",
                "containerVersionId": "0",
                "name": container_name,
                "description": "Generated by Versya GTM Container Generator",
                "container": {
                    "path": "accounts/0/containers/0",
                    "accountId": "0",
                    "containerId": "0",
                    "name": container_name,
                    "publicId": gtm_container_id or "GTM-XXXXXX",
                    "usageContext": ["WEB"]
                },
                "tag": [],
                "trigger": [],
                "variable": [],
                "folder": [],
                "builtInVariable": [
                    {"type": "PAGE_URL"},
                    {"type": "PAGE_HOSTNAME"},
                    {"type": "PAGE_PATH"},
                    {"type": "REFERRER"},
                    {"type": "EVENT"}
                ]
            }
        }

    def _generate_versya_sdk_tag(
        self,
        write_key: str,
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Generate Versya SDK initialization tag."""
        tag_id = self._next_tag_id()

        return {
            "accountId": "0",
            "containerId": "0",
            "tagId": tag_id,
            "name": "Versya SDK - Init",
            "type": "html",
            "parameter": [
                {
                    "type": "TEMPLATE",
                    "key": "html",
                    "value": f"""<script src="https://sdk.versya.io/v1/versya.js"></script>
<script>
  versya.init('{write_key}');
</script>"""
                },
                {
                    "type": "BOOLEAN",
                    "key": "supportDocumentWrite",
                    "value": "false"
                }
            ],
            "firingTriggerId": ["2147483647"],  # All Pages trigger
            "tagFiringOption": "ONCE_PER_EVENT",
            "monitoringMetadata": {"type": "MAP"}
        }

    def _generate_variables(
        self,
        event_schemas: List[MessagingEventSchema]
    ) -> List[Dict[str, Any]]:
        """Generate DataLayer variables for event properties."""
        variables = []

        # Common event properties
        common_props = [
            ("DLV - event_name", "event"),
            ("DLV - user_id", "user_id"),
            ("DLV - anonymous_id", "anonymous_id"),
            ("DLV - timestamp", "timestamp"),
            ("DLV - currency", "currency"),
            ("DLV - value", "value"),
            ("DLV - transaction_id", "transaction_id"),
            ("DLV - items", "items"),
        ]

        for name, data_layer_name in common_props:
            var_id = self._next_variable_id()
            variables.append({
                "accountId": "0",
                "containerId": "0",
                "variableId": var_id,
                "name": name,
                "type": "v",
                "parameter": [
                    {"type": "INTEGER", "key": "dataLayerVersion", "value": "2"},
                    {"type": "BOOLEAN", "key": "setDefaultValue", "value": "false"},
                    {"type": "TEMPLATE", "key": "name", "value": data_layer_name}
                ]
            })

        return variables

    def _generate_triggers(
        self,
        event_schemas: List[MessagingEventSchema]
    ) -> List[Dict[str, Any]]:
        """Generate triggers for each event schema."""
        triggers = []

        for schema in event_schemas:
            trigger_id = self._next_trigger_id()
            triggers.append({
                "accountId": "0",
                "containerId": "0",
                "triggerId": trigger_id,
                "name": f"Event - {schema.display_name or schema.event_name}",
                "type": "CUSTOM_EVENT",
                "customEventFilter": [
                    {
                        "type": "EQUALS",
                        "parameter": [
                            {"type": "TEMPLATE", "key": "arg0", "value": "{{_event}}"},
                            {"type": "TEMPLATE", "key": "arg1", "value": schema.event_name}
                        ]
                    }
                ]
            })

        return triggers

    def _generate_destination_tags(
        self,
        destination: MessagingDestination,
        mappings: List[MessagingEventMapping]
    ) -> List[Dict[str, Any]]:
        """Generate tags for a destination."""
        tags = []

        # Decrypt config
        config = destination_config.decrypt_config(destination.config_encrypted) or {}

        if destination.destination_type == DestinationType.ga4:
            tags.extend(self._generate_ga4_tags(destination, config, mappings))
        elif destination.destination_type == DestinationType.meta_pixel:
            tags.extend(self._generate_meta_pixel_tags(destination, config, mappings))
        elif destination.destination_type == DestinationType.google_ads:
            tags.extend(self._generate_google_ads_tags(destination, config, mappings))

        return tags

    def _generate_ga4_tags(
        self,
        destination: MessagingDestination,
        config: Dict[str, Any],
        mappings: List[MessagingEventMapping]
    ) -> List[Dict[str, Any]]:
        """Generate GA4 tags."""
        tags = []
        measurement_id = config.get("measurement_id", "G-XXXXXXXX")

        # Config tag
        config_tag_id = self._next_tag_id()
        tags.append({
            "accountId": "0",
            "containerId": "0",
            "tagId": config_tag_id,
            "name": f"GA4 Config - {destination.name}",
            "type": "gaawc",
            "parameter": [
                {"type": "TEMPLATE", "key": "measurementId", "value": measurement_id},
                {"type": "BOOLEAN", "key": "sendPageView", "value": "true"}
            ],
            "firingTriggerId": ["2147483647"],  # All Pages
            "tagFiringOption": "ONCE_PER_PAGE"
        })

        # Event tags for each mapping
        for mapping in mappings:
            tag_id = self._next_tag_id()
            tags.append({
                "accountId": "0",
                "containerId": "0",
                "tagId": tag_id,
                "name": f"GA4 Event - {mapping.destination_event_name}",
                "type": "gaawe",
                "parameter": [
                    {"type": "TAG_REFERENCE", "key": "measurementId", "value": config_tag_id},
                    {"type": "TEMPLATE", "key": "eventName", "value": mapping.destination_event_name},
                    {"type": "LIST", "key": "eventParameters", "list": self._map_properties_ga4(mapping.property_mappings or [])}
                ],
                "firingTriggerId": [str(mapping.event_schema_id)]  # Simplified - would need proper trigger lookup
            })

        return tags

    def _generate_meta_pixel_tags(
        self,
        destination: MessagingDestination,
        config: Dict[str, Any],
        mappings: List[MessagingEventMapping]
    ) -> List[Dict[str, Any]]:
        """Generate Meta Pixel tags."""
        tags = []
        pixel_id = config.get("pixel_id", "XXXXXXXXXX")

        # Base pixel tag
        base_tag_id = self._next_tag_id()
        tags.append({
            "accountId": "0",
            "containerId": "0",
            "tagId": base_tag_id,
            "name": f"Meta Pixel - {destination.name}",
            "type": "html",
            "parameter": [
                {
                    "type": "TEMPLATE",
                    "key": "html",
                    "value": f"""<!-- Meta Pixel Code -->
<script>
!function(f,b,e,v,n,t,s)
{{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};
if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
n.queue=[];t=b.createElement(e);t.async=!0;
t.src=v;s=b.getElementsByTagName(e)[0];
s.parentNode.insertBefore(t,s)}}(window,document,'script',
'https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '{pixel_id}');
fbq('track', 'PageView');
</script>
<!-- End Meta Pixel Code -->"""
                }
            ],
            "firingTriggerId": ["2147483647"],
            "tagFiringOption": "ONCE_PER_PAGE"
        })

        return tags

    def _generate_google_ads_tags(
        self,
        destination: MessagingDestination,
        config: Dict[str, Any],
        mappings: List[MessagingEventMapping]
    ) -> List[Dict[str, Any]]:
        """Generate Google Ads conversion tags."""
        tags = []
        conversion_id = config.get("conversion_id", "AW-XXXXXXXXX")

        # Conversion tag for each mapping
        for mapping in mappings:
            tag_id = self._next_tag_id()
            conversion_label = config.get("conversion_label", "")

            tags.append({
                "accountId": "0",
                "containerId": "0",
                "tagId": tag_id,
                "name": f"Google Ads - {mapping.destination_event_name}",
                "type": "awct",
                "parameter": [
                    {"type": "TEMPLATE", "key": "conversionId", "value": conversion_id},
                    {"type": "TEMPLATE", "key": "conversionLabel", "value": conversion_label},
                    {"type": "TEMPLATE", "key": "conversionValue", "value": "{{DLV - value}}"},
                    {"type": "TEMPLATE", "key": "currencyCode", "value": "{{DLV - currency}}"},
                    {"type": "TEMPLATE", "key": "orderId", "value": "{{DLV - transaction_id}}"}
                ],
                "firingTriggerId": [str(mapping.event_schema_id)]
            })

        return tags

    def _map_properties_ga4(
        self,
        property_mappings: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert property mappings to GA4 parameter format."""
        params = []
        for mapping in property_mappings:
            params.append({
                "type": "MAP",
                "map": [
                    {"type": "TEMPLATE", "key": "name", "value": mapping.get("target", "")},
                    {"type": "TEMPLATE", "key": "value", "value": f"{{{{DLV - {mapping.get('source', '')}}}}}"}
                ]
            })
        return params


# Singleton instance
gtm_generator = GTMGenerator()
