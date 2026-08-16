document.addEventListener("DOMContentLoaded", function () {
    const typeSelect = document.querySelector("#id_location_type");
    const regionRow = document.querySelector(".field-region");
    const structureRow = document.querySelector(".field-structure_id");

    function toggleFields() {
        if (!typeSelect) return;

        if (typeSelect.value === "region") {
            if (regionRow) regionRow.style.display = "";
            if (structureRow) structureRow.style.display = "none";
        } else {
            if (regionRow) regionRow.style.display = "none";
            if (structureRow) structureRow.style.display = "";
        }
    }

    if (typeSelect) {
        toggleFields();
        typeSelect.addEventListener("change", toggleFields);
    }
});
