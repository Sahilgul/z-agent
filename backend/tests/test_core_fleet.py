import pytest

from app.core.fleet import get_fleet_config


def test_get_fleet_config_returns_repos_and_graph():
    get_fleet_config.cache_clear()
    repos, graph = get_fleet_config()
    names = {r.name for r in repos}
    assert len(repos) >= 1
    assert hasattr(graph, "services")
    assert hasattr(graph, "call_chain")
    assert isinstance(graph.live_services, list)
    assert isinstance(graph.blast_radius_rules, list)


def test_fleet_graph_blast_radius_for_known_service():
    get_fleet_config.cache_clear()
    _, graph = get_fleet_config()
    services = list(graph.services)
    if not services:
        return
    target = services[0]
    affected = graph.blast_radius_for(target)
    assert isinstance(affected, list)


def test_fleet_graph_condensed_for_prompt_shape():
    get_fleet_config.cache_clear()
    _, graph = get_fleet_config()
    text = graph.condensed_for_prompt()
    assert "Fleet call chain:" in text
    assert "Blast-radius rules:" in text


def test_get_fleet_config_cached():
    get_fleet_config.cache_clear()
    a = get_fleet_config()
    b = get_fleet_config()
    assert a is b
