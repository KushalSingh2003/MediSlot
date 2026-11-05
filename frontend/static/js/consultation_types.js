
document.addEventListener("DOMContentLoaded", function () {

    const typeSelect = document.getElementById("consultationTypeId");
    const capacityInput = document.getElementById("capacity");
    const capacityHelp = document.getElementById("capacityHelp");

    if (!typeSelect || !capacityInput) return;

    typeSelect.addEventListener("change", function () {

        const selectedOption = typeSelect.options[typeSelect.selectedIndex];
        const typeName = selectedOption.dataset.name;

        if (typeName === "Health Camp Session") {
            capacityInput.min = 2;
            capacityInput.max = 10;
            capacityInput.value = 2;

            capacityHelp.textContent =
                "Health Camp Session supports 2–10 patients.";

        } else if (typeName) {
            capacityInput.min = 1;
            capacityInput.max = 1;
            capacityInput.value = 1;

            capacityHelp.textContent =
                "This consultation supports exactly 1 patient.";
        } else {
            capacityInput.min = 1;
            capacityInput.max = 10;
            capacityInput.value = 1;

            capacityHelp.textContent =
                "Select a consultation type to configure capacity.";
        }
    });

    document.getElementById("addConsultationForm")
        ?.addEventListener("submit", function (event) {

            const capacity = Number(capacityInput.value);
            const selectedOption =
                typeSelect.options[typeSelect.selectedIndex];

            const typeName = selectedOption.dataset.name;

            if (
                typeName === "Health Camp Session" &&
                (capacity < 2 || capacity > 10)
            ) {
                event.preventDefault();
                alert("Health Camp capacity must be between 2 and 10.");
                return;
            }

            if (
                typeName &&
                typeName !== "Health Camp Session" &&
                capacity !== 1
            ) {
                event.preventDefault();
                alert("This consultation requires capacity exactly 1.");
            }
        });

});
