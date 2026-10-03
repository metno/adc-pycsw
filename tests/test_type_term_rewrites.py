from pycsw_solr_metno.solr_metno import SolrMETNORepository


def _rewrite(data):
    repo = SolrMETNORepository.__new__(SolrMETNORepository)
    return repo._rewrite_type_terms(data)


def test_top_level_item_type_becomes_non_parent():
    assert _rewrite({"query": 'isChild:"item"'}) == {"query": "isParent:false"}


def test_item_type_inside_bool_clause():
    query = {"query": {"bool": {"must": ["full_text:*Svalbard*", 'isChild:"item"']}}}
    assert _rewrite(query) == {
        "query": {"bool": {"must": ["full_text:*Svalbard*", "isParent:false"]}}
    }


def test_dataset_and_series_types_combined_with_other_clauses():
    query = {
        "query": {"bool": {"must": ['isChild:"dataset"']}},
        "filter": ["{!field f=geospatial_bounds3d v='Intersects(ENVELOPE(0, 1, 1, 0))'}"],
    }
    assert _rewrite(query)["query"] == {"bool": {"must": ["isParent:false"]}}
    assert _rewrite({"query": 'isChild:"series"'}) == {"query": "isParent:true"}


def test_stac_collection_typename():
    assert _rewrite({"query": 'typename:"stac:Collection"'}) == {
        "query": "isParent:true"
    }


def test_negated_type_term_keeps_negation():
    assert _rewrite({"query": {"bool": {"must": ['-isChild:"series"']}}}) == {
        "query": {"bool": {"must": ["-isParent:true"]}}
    }


def test_other_terms_are_untouched():
    query = {"query": 'title:"isChild:\\"item\\""', "filter": ["isChild:true"]}
    assert _rewrite(query) == query
